# model-dispatch：让 Claude 当总指挥，把活派给 Gemini、GPT 和 DeepSeek

[English](README.en.md)

一个 Claude Code skill。你只跟 Claude 说话，Claude 负责拆解、派活、验收：

- **Gemini** 当只读顾问：独立出方案、改文案、挑错。
- **GPT（Codex）** 写接口已定的成块代码，并在上线前审查。
- **DeepSeek**（或任何 OpenAI 兼容的快模型）做几秒出结果的纯文字活。

```
              你：说需求 / 拍板
                      │
              Claude（总指挥）
     拆解 · 前端视觉 · 小改动 · 验收 · 汇报
   ┌────────────┬─────────┴──────┬────────────┐
 gemini-ask   codex-worker   codex-review    fast-ask
 Gemini       GPT 写代码      GPT 审查        快模型
 方案/文案    成块独立模块    挡上线/建议     秒级文字活
```

思路来自数字生命卡兹克的文章《毫无保留地分享一下，我是如何让Claude、GPT和Gemini完美协同的。》。这个仓库是我照着它在自己电脑上搭了一版、用了几天之后整理出来的通用版本。

## 它和「直接让 Claude 调别的模型」有什么不同

光有分工规则不够，模型会偷懒、会出错、会挂。这个 skill 用脚本把这些情况拦住：

| 情况 | 脚本怎么做 |
|---|---|
| Gemini 某个模型额度用完 | 自动换下一个模型；全挂了由快模型替补，输出开头写明是替补 |
| 让 Gemini 只读，它却改了文件 | 运行前后比对目录指纹，有变化就警告 |
| Codex 交了报告，但一个文件都没改 | 判失败（退出码 4），不让「假成功」混过去 |
| 交回后分不清哪些是 Codex 改的 | 派活前要求 git 工作树干净；交回时打印变化统计和会话 ID |
| 审查意见一大堆，不知道哪些必须修 | 结论分两级：「挡上线」必须修，「建议」记下即可 |
| 改到钱、登录、发消息的代码 | 审查自动升到深档推理 |
| 修了又审、审了又修，没完没了 | 自动数轮次，最多两轮，第二轮还挡就交给你决定 |
| Codex 挂了，审查做不了 | 快模型替补审查，并提示你核实挡上线条目 |
| 不小心把客户资料发给外部模型 | 配置敏感词；任务文字、目录里文件的名字和内容命中，就拒绝外发（退出码 3） |
| 事后想知道派了什么活 | 每次派活留一个目录，`md-jobs` 一眼看完 |

## 安装

需要：Python 3.10+、git、Claude Code。各通道按需装：

- Gemini：官方 [Gemini CLI](https://github.com/google-gemini/gemini-cli)（`gemini` 命令）。
- GPT：[Codex CLI](https://github.com/openai/codex)（`codex` 命令），用 ChatGPT 账号或 API key 登录。
- 快模型：设环境变量 `DEEPSEEK_API_KEY`（或 `FAST_API_KEY`，配合 `fast_base_url` 用别家）。

```bash
git clone https://github.com/hanzo-genji233/model-dispatch.git ~/model-dispatch
```

```bash
~/model-dispatch/install.sh
```

安装脚本做三件事：

1. 在 `~/.local/bin` 建 5 个命令。
2. 把 skill 软链到 `~/.claude/skills/model-dispatch`。
3. 在 `~/.claude/CLAUDE.md` 末尾加一行，引用分工规则 `rules/CLAUDE.md`。

装完会打印体检结果，告诉你哪个通道还没装好。

## 用法

平时不用记命令，直接对 Claude 说：「让 Gemini 也出一份方案」「这段派给 Codex 写」「上线前审查一下」。Claude 会按规则自己调用。

也可以手动用：

```bash
gemini-ask --plan . "我们的定时任务失败了怎么自动恢复？给一个方案"
```

```bash
codex-worker ./my-repo "实现 report.py：读 data.csv，按月汇总金额，写测试"
```

```bash
codex-review ./my-repo
```

```bash
fast-ask "把下面 20 条用户反馈分成 5 类：..."
```

```bash
md-jobs
```

完整参数、退出码、配置项见 [SKILL.md](SKILL.md)。

## 配置

编辑 `~/.config/model-dispatch/config.json`（示例 [config.example.json](config.example.json)）。最常改的：

- `gemini_models`：按你账号实际能用的填，比如 `["gemini-2.5-pro", "gemini-2.5-flash"]`，按顺序尝试。
- `gemini_cmd`：想把「顾问」换成别的命令行 agent，改这个命令模板。
- `codex_models`：按你账号实际能用的填，比如 `["gpt-5-codex"]`。空着就用 Codex 自己的默认。
- `sensitive_markers`：你的客户名、项目代号。

## 规则文件

`rules/CLAUDE.md` 是这套东西的另一半：什么时候派、派给谁、怎么验收。其中「改全与核实」一节也改写自卡兹克的规则，治 AI 最常见的毛病：改不全、漏细节、没看到结果就说做完。按你自己的习惯改它。

## 测试

```bash
python3 scripts/test_md.py
```

离线跑，不调任何模型，约 2 秒。每一条测试都对应一个真实踩过的坑。

## 已知限制

- 敏感词拦截是防手滑，不是安全边界：只认你配的词；二进制文件（含 PDF、Office 文档）和 `.git`、`node_modules`、`.venv` 等目录不扫。
- 目标目录超过 5000 个文件或 50 MB 文本时扫不完，脚本默认拦截；确认能外发再加 `--allow-sensitive`。
- Gemini 的「只读」靠提示词和目录指纹警告，不是沙箱。
- 快模型替补看不到目录，只看得到任务文字和任务里点名的文件。
- 主要在 macOS 上用过；Linux 应该能跑，Windows 没测。

## 许可

MIT
