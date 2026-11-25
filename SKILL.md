---
name: model-dispatch
description: "多模型分工派活：总指挥（通常是 Claude Code）把活派给 Gemini（gemini-ask：独立方案 / 文案交互顾问 / 挑错，只读）、GPT（codex-worker 写成块代码、codex-review 两级代码审查）、快模型（fast-ask，默认 DeepSeek，几秒出结果的纯文字活）。触发：让 Gemini 也出一份方案、请 Gemini 改文案、派给 Codex / GPT 写、上线前审查一下、让 DeepSeek 快速处理、多模型协同、看看派出去的活。"
---

# 多模型派活（model-dispatch）

```
              用户：说需求 / 拍板
                      │
              Claude（总指挥）
     拆解 · 前端视觉 · 小改动 · 验收 · 汇报
   ┌────────────┬─────────┴──────┬────────────┐
 gemini-ask   codex-worker   codex-review    fast-ask
 Gemini       GPT 写代码      GPT 审查        快模型
 方案/文案    成块独立模块    挡上线/建议     秒级文字活
```

分工规则（什么时候派、派给谁、怎么验收）在 `rules/CLAUDE.md`，安装时接进 `~/.claude/CLAUDE.md`。

## 命令

任务文字可以直接写、`@文件`、或 `-` 读标准输入。所有命令都要用后台运行，完成后再读结果。

| 命令 | 做什么 | 关键点 |
|---|---|---|
| `gemini-ask [--plan\|--copy\|--review] <目录> "任务"` | Gemini 只读顾问 | 默认调官方 `gemini` 命令行；模型额度用完自动换下一个；全挂时由快模型替补（输出开头会注明）；运行前后比对目录指纹，被改动会警告 |
| `codex-worker [--resume ID] <git目录> "任务"` | GPT 写代码 | 工作区沙箱；要求干净的 git 工作树（`--allow-dirty` 放行）；一个文件都没改判失败（退出码 4，`--allow-noop` 放行）；交回时打印报告、变化统计和会话 ID |
| `codex-review [--base 分支\|--commit SHA] [--deep] [仓库]` | GPT 审查 | 默认审未提交改动（含未跟踪文件，不碰真实 .git/index）；结论分「挡上线 / 建议」；改到钱、登录、发消息的代码自动升深档；自动数轮次，第二轮还挡就提示交给用户；有挡上线问题退出码 10；Codex 挂了由快模型替补 |
| `fast-ask "任务"` | 快模型纯文字活 | OpenAI 兼容接口，默认 DeepSeek；不碰文件 |
| `md-jobs [-n 15]` | 最近派出去的活 | 时间、成败、用时、模型 / 轮次 |

通用参数：`--allow-sensitive`（确认敏感资料可以外发）、`--timeout 秒`、`--model`。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 模型没交回结果（看任务目录里的 `stderr.txt`） |
| 2 | 参数或环境不对 |
| 3 | 敏感词拦截 |
| 4 | codex-worker 没改任何文件 |
| 10 | codex-review 有挡上线问题 |

## 配置

`~/.config/model-dispatch/config.json`，键名同 `scripts/md.py` 里的 `DEFAULTS`，示例见 `config.example.json`。常改的：

- `gemini_models`：按顺序尝试的 Gemini 模型。
- `gemini_cmd`：顾问命令模板。想换成别的命令行 agent，改这里。
- `codex_models`、`codex_effort`：Codex 模型和推理档位。
- `fast_base_url`、`fast_model`、`fast_key_env`：快模型接口。
- `sensitive_markers`：客户名、项目代号等。命中就拒绝外发。

## 记录

每个任务一个目录：`~/.local/share/model-dispatch/jobs/<时间>-<命令>-<摘要>/`，含 `task.md`、`output.md` / `review.md`、`meta.json`、`stderr.txt`。总表在 `jobs.jsonl`。

## 坑点

- 敏感词拦截是防手滑，不是安全边界。总指挥自己仍要判断内容能不能外发。
- `gemini-ask` 的只读靠提示词和目录指纹警告。看到「目录有变化」要去查 `git status`。
- 快模型替补看不到目录，只能看到任务文字和任务里点名的文件。
- 别的模型交回的东西，总指挥核实过才算数：读 diff、自己跑测试。
- 改 `scripts/md.py` 后跑 `python3 scripts/test_md.py`（离线，约 2 秒）。
