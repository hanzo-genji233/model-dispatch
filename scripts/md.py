#!/usr/bin/env python3
"""model-dispatch：让一个总指挥 agent（通常是 Claude Code）把活派给别的模型。

同一个文件，按调用名分派（install.sh 会在 ~/.local/bin 建软链）：
  gemini-ask    Gemini 只读顾问：独立方案 / 文案交互 / 挑错（默认调官方 gemini 命令行，可换成任何命令）
  codex-worker  GPT（Codex）写代码工人：工作区沙箱，可 --resume
  codex-review  GPT 代码审查：两级结论（挡上线 / 建议），最多两轮
  fast-ask      快模型（默认 DeepSeek，任何 OpenAI 兼容接口都行）的纯文字活，几秒出结果
  md-jobs       查看最近派出去的活
也可以 `md.py <子命令> ...` 调用。

每个任务记录在 ~/.local/share/model-dispatch/（每个任务一个目录 + jobs.jsonl）。
配置写在 ~/.config/model-dispatch/config.json，键名同下面的 DEFAULTS。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
DATA = Path(os.environ.get("MD_DATA_DIR", HOME / ".local/share/model-dispatch"))
JOBS = DATA / "jobs"
JOBLOG = DATA / "jobs.jsonl"
CONFIG = Path(os.environ.get("MD_CONFIG", HOME / ".config/model-dispatch/config.json"))

DEFAULTS: dict = {
    # ---- Gemini 顾问通道 ----
    # 命令模板。{prompt} 换成完整提示词；模板里没有 {prompt} 时，提示词从标准输入送进去。
    # {model} 所在的那一对参数（如 "-m", "{model}"）在没指定模型时会整对去掉。
    # 想换成别的顾问（claude -p、opencode run、自己的脚本），改这里就行。
    "gemini_cmd": ["gemini", "-m", "{model}", "-p", "{prompt}"],
    # 按顺序尝试；某个模型额度用完 / 不支持就换下一个。空列表 = 用命令行自己的默认模型。
    "gemini_models": [],
    # 输出里出现这些字样，算「这个模型现在用不了」，换下一个模型。
    "gemini_retry_markers": ["RESOURCE_EXHAUSTED", "quota", "429", "rate limit", "not supported", "model not found"],
    # ---- Codex 通道 ----
    "codex_models": [],              # 空 = 用 codex 自己的默认模型
    "codex_effort": "high",
    "review_effort": "high",
    "review_deep_effort": "xhigh",
    # 无头调用时追加的参数。默认清空 MCP：无人值守时 MCP 工具可能绕过沙箱，也拖慢启动。
    "codex_extra_args": ["-c", "mcp_servers={}"],
    # ---- 快模型通道（OpenAI 兼容 /chat/completions）----
    "fast_base_url": "https://api.deepseek.com/v1",
    "fast_model": "deepseek-chat",
    "fast_key_env": ["FAST_API_KEY", "DEEPSEEK_API_KEY"],
    # Gemini 全挂时，用快模型替补（只能看任务文字和任务里点名的文件，不能自己翻目录）
    "fallback_to_fast": True,
    # ---- 敏感资料拦截 ----
    # 任务、点名文件、仓库 remote、目录文件名、diff 里出现这些词就拒绝外发（退出码 3）。
    # 写你的客户名、项目代号、内部域名。默认空 = 不拦。
    "sensitive_markers": [],
    # 改到这些路径或增删行内容时，审查自动用深档推理
    "deep_review_patterns": [
        r"pay", r"money", r"price", r"refund", r"ledger", r"invoice",
        r"auth", r"login", r"token", r"secret", r"password", r"credential", r"permission",
        r"send", r"notify", r"webhook", r"mail", r"sms",
    ],
}


def cfg() -> dict:
    c = dict(DEFAULTS)
    if CONFIG.exists():
        try:
            c.update(json.loads(CONFIG.read_text()))
        except Exception as e:  # 配置坏了不影响跑，但要说出来
            print(f"[md] 配置文件读不了，用默认值：{CONFIG}：{e}", file=sys.stderr)
    return c


def as_list(v) -> list[str]:
    if not v:
        return []
    return list(v) if isinstance(v, list) else [str(v)]


# ---------------------------------------------------------------- 公共

def read_task(raw: str) -> str:
    """任务文字：直接写、@文件、或 - 读标准输入。"""
    if raw == "-":
        return sys.stdin.read()
    if raw.startswith("@") and Path(raw[1:]).expanduser().is_file():
        return Path(raw[1:]).expanduser().read_text()
    return raw


def slug(text: str) -> str:
    s = re.sub(r"[^\w一-鿿]+", "-", text.strip())[:24].strip("-")
    return s or "task"


def new_job(tool: str, task: str) -> Path:
    JOBS.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    d = JOBS / f"{ts}-{tool}-{slug(task)}"
    seq = 2
    while d.exists():
        d = JOBS / f"{ts}-{seq}-{tool}-{slug(task)}"
        seq += 1
    d.mkdir()
    (d / "task.md").write_text(task)
    return d


def log_job(job: Path, **meta) -> None:
    meta.setdefault("job", str(job))
    meta.setdefault("time", dt.datetime.now().isoformat(timespec="seconds"))
    (job / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    DATA.mkdir(parents=True, exist_ok=True)
    with JOBLOG.open("a") as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")


PATH_PREFIX = r"(?:~/|/(?:Users|home|private|tmp|Volumes|var|etc|opt|mnt|srv)/)"


def task_paths(task: str) -> list[str]:
    """提取任务文字里点名的、真实存在的文件（支持含空格的路径）。只认文件，不认目录。"""
    found: list[str] = []

    def add(cand: str) -> bool:
        cleaned = cand.strip().rstrip(".:,;，。；：")
        if not cleaned:
            return False
        try:
            q = Path(cleaned).expanduser()
            if q.is_file():
                s = str(q)
                if s not in found:
                    found.append(s)
                return True
        except OSError:
            pass
        return False

    # 1) 反引号或引号包裹的路径
    for m in re.finditer(rf"[`'\"]({PATH_PREFIX}[^`'\"\r\n]+)[`'\"]", task):
        add(m.group(1))
    # 2) 没加引号的路径：截到行尾或标点，按空白由长到短找最长的真实文件
    for m in re.finditer(PATH_PREFIX, task):
        line = re.split(r"[\r\n`'\"，。；！？、<>|]", task[m.start():], maxsplit=1)[0]
        cuts = [len(line)] + [ws.start() for ws in re.finditer(r"\s+", line)]
        for end in sorted(set(cuts), reverse=True):
            seg = line[:end]
            if add(seg) or add(seg.rstrip(")）】]")):
                break
    return found[:30]


def collect_guard_texts(
    base_texts: list[str],
    target_dir: Path | None = None,
    extra_files: list[str] | None = None,
    include_dir_files: bool = False,
) -> list[str]:
    """汇总要过敏感拦截的文本：提示词、目录路径、点名文件内容、git remote、目录内文件名。"""
    out = list(base_texts)
    for fp in extra_files or []:
        try:
            out.append(Path(fp).read_text(errors="ignore")[:200_000])
        except OSError:
            pass
    if target_dir and target_dir.is_dir():
        resolved = target_dir.resolve()
        if is_git(resolved):
            out.append(git(resolved, "remote", "-v"))
        if include_dir_files and as_list(cfg().get("sensitive_markers")):
            out.extend(_dir_texts(resolved))
    return out


# 目录内容扫描上限：外部模型能在目录里读到的文件，名字和内容都要过敏感词。
# 扫不完（超上限）就默认拦截，不静默放行；确认能外发再加 --allow-sensitive。
_SCAN_MAX_FILES = 5000
_SCAN_MAX_TOTAL_BYTES = 50_000_000
_SCAN_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}
_SCAN_INCOMPLETE = "\x00md-scan-incomplete:"


def _dir_texts(root: Path) -> list[str]:
    """目录里的文件名 + 文本文件内容（含 gitignore 掉的文件、符号链接指向的文件，模型一样读得到）。

    只跳过二进制文件。超过文件数或总字节上限时，在结果里放一条「没扫完」标记，sensitive_guard 见到就拦截。
    """
    names: list[str] = []
    contents: list[str] = []
    total = 0
    count = 0
    seen_dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen_dirs:  # 符号链接成环
            dirnames[:] = []
            continue
        seen_dirs.add(real)
        dirnames[:] = [d for d in dirnames if d not in _SCAN_SKIP_DIRS]
        names.append(os.path.relpath(dirpath, root))
        for fn in filenames:
            count += 1
            if count > _SCAN_MAX_FILES:
                contents.append("\n".join(names))
                contents.append(f"{_SCAN_INCOMPLETE}目录里文件超过 {_SCAN_MAX_FILES} 个")
                return contents
            names.append(fn)
            fp = Path(dirpath) / fn  # 符号链接也读：read_bytes 会跟到真实文件
            try:
                size = fp.stat().st_size
            except OSError:
                continue  # 断链或读不了：模型也读不到
            if total + size > _SCAN_MAX_TOTAL_BYTES:
                contents.append("\n".join(names))
                contents.append(f"{_SCAN_INCOMPLETE}目录文本超过 {_SCAN_MAX_TOTAL_BYTES // 1_000_000} MB")
                return contents
            try:
                data = fp.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue  # 二进制
            total += len(data)
            contents.append(data.decode("utf-8", errors="ignore"))
    contents.append("\n".join(names))
    return contents


def sensitive_guard(texts: list[str], allowed: bool, who: str) -> None:
    """命中敏感词就拦下。这是防手滑，不是安全边界。"""
    if allowed:
        return
    markers = [m.lower() for m in as_list(cfg().get("sensitive_markers"))]
    if not markers:
        return
    incomplete = [t[len(_SCAN_INCOMPLETE):] for t in texts if t.startswith(_SCAN_INCOMPLETE)]
    if incomplete:
        die(
            f"拦截：{incomplete[0]}，敏感词没法扫完，会发给 {who}。\n"
            "确认目录里没有不能外发的资料后，加 --allow-sensitive 重跑；或者把任务目录缩小再派。",
            code=3,
        )
    blob = "\n".join(texts).lower()
    hits = sorted({m for m in markers if m in blob})
    if hits:
        die(
            f"拦截：任务、文件或仓库里有敏感词 {hits}，会发给 {who}。\n"
            "确认可以外发后，加 --allow-sensitive 重跑；或者去掉这些资料再派。",
            code=3,
        )


def die(msg: str, code: int = 2):
    print(f"[md] {msg}", file=sys.stderr)
    sys.exit(code)


def run(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int | None = None,
    stdin_text: str | None = None,
) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd, cwd=cwd, timeout=timeout, text=True, capture_output=True,
            input=stdin_text if stdin_text is not None else "",
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        return 124, out, "超时"
    except FileNotFoundError:
        return 127, "", f"找不到命令：{cmd[0]}"


def git(repo: Path, *args: str) -> str:
    # core.quotepath=false：中文路径不转成八进制转义，敏感词才匹配得上
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", "-C", str(repo), *args],
        text=True, capture_output=True,
    ).stdout


def is_git(repo: Path) -> bool:
    return git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"


def has_git_head(repo: Path) -> bool:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"], capture_output=True,
    ).returncode == 0


def working_tree_diff(repo: Path) -> str:
    """用临时 GIT_INDEX_FILE 生成含未跟踪文件的完整 diff，不碰真实的 .git/index。"""
    idx_raw = git(repo, "rev-parse", "--git-path", "index").strip()
    real_idx = (repo / idx_raw).resolve()
    with tempfile.TemporaryDirectory(prefix="md-gitidx-") as td:
        tmp_idx = Path(td) / "index"
        if real_idx.is_file():
            shutil.copy2(real_idx, tmp_idx)
        env = {**os.environ, "GIT_INDEX_FILE": str(tmp_idx)}
        subprocess.run(["git", "-C", str(repo), "add", "-N", "."], env=env, capture_output=True)
        args = ["git", "-c", "core.quotepath=false", "-C", str(repo), "diff"]
        if has_git_head(repo):
            args.append("HEAD")
        return subprocess.run(args, env=env, text=True, capture_output=True).stdout


def fingerprint(d: Path) -> str:
    """目录指纹：git 仓库看状态 + diff + 未跟踪文件大小和时间；普通目录看文件大小和时间。"""
    if is_git(d):
        parts = [
            git(d, "status", "--porcelain"),
            git(d, "diff", "HEAD") if has_git_head(d) else git(d, "diff"),
        ]
        for rel in git(d, "ls-files", "--others", "--exclude-standard").splitlines():
            if not rel:
                continue
            try:
                st = (d / rel).stat()
                parts.append(f"??|{rel}|{st.st_size}|{st.st_mtime_ns}")
            except OSError:
                pass
        return "\n".join(parts)
    rows = []
    for i, p in enumerate(sorted(d.rglob("*"))):
        if i > 5000:
            break
        try:
            st = p.stat()
            rows.append(f"{p}|{st.st_size}|{st.st_mtime_ns}")
        except OSError:
            pass
    return "\n".join(rows)


# ---------------------------------------------------------------- fast-ask（OpenAI 兼容接口）

FAST_RULES = "【规则】这是一次纯文字任务。用中文，先给结论，简短。"


def fast_key(c: dict) -> str | None:
    for name in as_list(c.get("fast_key_env")):
        v = os.environ.get(name)
        if v:
            return v
    return None


def fast_chat(c: dict, prompt: str, timeout: int) -> str:
    key = fast_key(c)
    if not key:
        raise RuntimeError(f"没有快模型的 key：请设置环境变量 {' 或 '.join(as_list(c.get('fast_key_env')))}")
    body = {"model": c["fast_model"], "messages": [{"role": "user", "content": prompt}], "stream": False}
    req = urllib.request.Request(
        c["fast_base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"快模型接口报错 {e.code}：{e.read()[:300].decode(errors='ignore')}") from None
    return data["choices"][0]["message"]["content"]


def cmd_fast_ask(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="fast-ask", description="快模型纯文字活：改写、分类、抽取、快速第二意见。不碰文件。")
    ap.add_argument("--allow-sensitive", action="store_true", help="确认敏感资料可以外发")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("task", help="任务文字；@文件 或 - 读标准输入")
    a = ap.parse_args(argv)
    c = cfg()
    task = read_task(a.task)
    sensitive_guard(collect_guard_texts([task]), a.allow_sensitive, f"快模型（{c['fast_model']}）")
    job = new_job("fast-ask", task)
    t0 = time.time()
    try:
        out = fast_chat(c, f"{FAST_RULES}\n\n【任务】\n{task}", a.timeout)
    except Exception as e:
        log_job(job, tool="fast-ask", model=c["fast_model"], ok=False, seconds=round(time.time() - t0))
        die(f"{e}\n记录：{job}", code=1)
    (job / "output.md").write_text(out)
    log_job(job, tool="fast-ask", model=c["fast_model"], ok=bool(out.strip()), seconds=round(time.time() - t0))
    print(out.strip())
    print(f"\n[md] 用时 {round(time.time() - t0)} 秒，记录 {job}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- gemini-ask

ASK_RULES = """【回答规则】
- 直接在回复正文给出完整答案。不要写计划文件，不要改任何文件。
- 关于代码 / 文件 / 数据的判断，写出依据（文件名:行号或原文）。没亲眼核对过的，句首标「推测」。
- 先给结论，再给理由。不要客套，不要复述任务。
- 你是只读顾问：只读目标目录和任务里点名的文件，只用 ls / cat / grep / find / git log 这类只读操作。
  不要创建、修改、删除任何文件，不要联网。拿不到的信息直接标「推测」，不要去试。"""

ASK_MODES = {
    "plan": """【你的角色】独立方案顾问。另一位工程师也在独立出方案，你看不到他的草稿，也不需要迎合任何人。
请给出你认为最好的方案：目标拆解、关键做法、取舍理由、风险，以及你觉得对方可能会漏掉的地方。""",
    "copy": """【你的角色】界面文案与交互顾问。读者是不懂技术的普通人。
请逐条给出修改：原文 → 改后 → 一句理由。只改真正别扭、啰嗦、说不清、让人不知道下一步做什么的地方。
不要编造功能、承诺或数字；拿不准功能实际怎么用的，标「推测」并说明你假设了什么。最后给 3 条整体交互建议。""",
    "review": """【你的角色】挑错的审稿人。只找问题，不夸。
按严重程度排序，每条写：问题、依据（原文或文件:行号）、会造成什么后果、怎么改。没把握的标「推测」。""",
    "ask": "【你的角色】顾问。回答下面的问题。",
}


def build_cmd(template: list[str], prompt: str, model: str | None) -> tuple[list[str], str | None]:
    """按模板拼命令。返回（命令，要送进标准输入的文字或 None）。"""
    cmd: list[str] = []
    for tok in template:
        if "{model}" in tok and not model:
            # 没指定模型：去掉 {model}，以及紧挨在它前面的参数名（如 -m）
            if tok == "{model}" and cmd and cmd[-1].startswith("-"):
                cmd.pop()
            continue
        cmd.append(tok.replace("{model}", model or "").replace("{prompt}", prompt))
    uses_prompt = any("{prompt}" in t for t in template)
    return cmd, (None if uses_prompt else prompt)


def looks_unavailable(text: str, markers: list[str]) -> bool:
    low = text.lower()
    return any(m.lower() in low for m in markers)


def cmd_gemini_ask(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="gemini-ask", description="请 Gemini 当只读顾问（不改文件）。")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--plan", action="store_const", const="plan", dest="mode", help="独立出方案（只给原话和必要背景，不给自己的草稿）")
    g.add_argument("--copy", action="store_const", const="copy", dest="mode", help="界面文案 / 交互顾问")
    g.add_argument("--review", action="store_const", const="review", dest="mode", help="挑错")
    ap.add_argument("--model", help="指定模型（默认按 gemini_models 顺序尝试）")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--allow-sensitive", action="store_true", help="确认敏感资料可以外发")
    ap.add_argument("--no-fallback", action="store_true", help="Gemini 全挂时不用快模型替补")
    ap.add_argument("dir", help="Gemini 工作的目录（只读）")
    ap.add_argument("task", help="任务文字；@文件 或 - 读标准输入")
    a = ap.parse_args(argv)
    mode = a.mode or "ask"
    c = cfg()
    d = Path(a.dir).expanduser().resolve()
    if not d.is_dir():
        die(f"目录不存在：{d}")
    task = read_task(a.task)
    named = task_paths(task)
    sensitive_guard(collect_guard_texts([task, str(d)], target_dir=d, extra_files=named, include_dir_files=True),
                    a.allow_sensitive, "Google（Gemini）")

    prompt = f"{ASK_MODES[mode]}\n\n{ASK_RULES}\n\n【工作目录】{d}\n\n【任务】\n{task}"
    models: list[str | None] = [a.model] if a.model else (as_list(c.get("gemini_models")) or [None])
    job = new_job(f"gemini-{mode}", task)
    before = fingerprint(d)
    t0 = time.time()
    out, used, log = "", None, []
    for m in models:
        cmd, stdin_text = build_cmd(as_list(c["gemini_cmd"]), prompt, m)
        code, so, se = run(cmd, cwd=d, timeout=a.timeout, stdin_text=stdin_text)
        log.append(f"=== [{m or '默认模型'} exit={code}] ===\n{se.strip()}")
        if code == 0 and so.strip() and not looks_unavailable(so[:400], as_list(c["gemini_retry_markers"])):
            out, used = so, m or "默认模型"
            break
        if code == 127:
            log.append("（找不到 Gemini 命令行，跳过剩下的模型）")
            break
        if m is not models[-1]:
            print(f"[md] {m or '默认模型'} 没交回结果，换下一个模型", file=sys.stderr)

    fell_back = False
    if not out and c.get("fallback_to_fast") and not a.no_fallback and fast_key(c):
        print("[md] Gemini 都没交回结果，改用快模型替补", file=sys.stderr)
        files = "".join(
            f"\n\n【文件 {p}】\n{Path(p).read_text(errors='ignore')[:60_000]}" for p in named
        )
        try:
            body = fast_chat(c, f"{prompt}\n\n（你看不到目录，只能看到下面点名的文件。）{files}", a.timeout)
            out, used, fell_back = f"> 注意：Gemini 不可用，本结果由快模型 {c['fast_model']} 替补，它看不到目录里的其他文件。\n\n{body}", c["fast_model"], True
        except Exception as e:
            log.append(f"=== [fallback] ===\n{e}")

    (job / "stderr.txt").write_text("\n\n".join(log) + "\n")
    changed = fingerprint(d) != before
    ok = bool(out.strip())
    log_job(job, tool="gemini-ask", mode=mode, model=used, fallback=fell_back, ok=ok,
            dir_changed=changed, seconds=round(time.time() - t0))
    if not ok:
        die(f"没拿到结果。看 {job}/stderr.txt", code=1)
    (job / "output.md").write_text(out)
    print(out.strip())
    if changed:
        print(f"\n[md] 警告：目录 {d} 在 Gemini 运行期间有变化。它本应只读，请检查 git status。", file=sys.stderr)
    print(f"\n[md] 模型 {used}，用时 {round(time.time() - t0)} 秒，记录 {job}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- codex-worker

WORKER_RULES = """【你的身份】执行工人。总指挥已经定好接口和验收标准，你负责把它做完、做对。
【边界】
- 只改当前工作树里的文件。不装依赖、不联网、不 git commit / push、不改全局配置。
- 需要装依赖或联网才能验证的，写清楚要什么，交回来由总指挥做。
- 任务里写了「一起改的同类」，要全部改到。
【交回时汇报】（简短，中文）
1. 改了哪些文件，各做了什么。
2. 怎么验证的（命令 + 结果）。没验证的直说。
3. 没做完的、拿不准的、和任务描述不一致的地方。"""


def _is_model_unsupported(text: str) -> bool:
    low = text.lower()
    return "model_not_found" in low or "is not supported" in low


def _codex_errors(err: str, out: str = "") -> str:
    """找出 Codex 失败的原因。额度用完等报错写在标准输出的 JSON 事件里，不在 stderr。"""
    msgs: list[str] = []
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "error" and ev.get("message"):
            msgs.append(ev["message"])
    lines = [l for l in err.splitlines() if re.search(r"\b(error|failed|denied|panic)\b", l, re.I)]
    if not msgs and not lines and out.strip():
        lines = out.strip().splitlines()[-3:]
    return "\n".join(msgs[-1:] + lines)[-800:]


def _untracked_summary(wt: Path) -> str:
    rows = []
    for rel in git(wt, "ls-files", "--others", "--exclude-standard").splitlines():
        p = wt / rel
        if rel and p.is_file():
            try:
                rows.append(f" {rel} (新文件, {len(p.read_text(errors='ignore').splitlines())} 行)")
            except OSError:
                rows.append(f" {rel} (新文件)")
    return "\n".join(rows)


def model_args(m: str | None) -> list[str]:
    return ["-m", m] if m else []


def cmd_codex_worker(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="codex-worker", description="派 GPT（Codex）在工作区沙箱里写代码。")
    ap.add_argument("--resume", metavar="会话ID", help="接着上次的会话追加指令")
    ap.add_argument("--effort", help="推理档位，默认配置里的 codex_effort")
    ap.add_argument("--model", help="指定模型（默认按 codex_models 顺序尝试）")
    ap.add_argument("--timeout", type=int, default=3600)
    ap.add_argument("--allow-sensitive", action="store_true")
    ap.add_argument("--allow-dirty", action="store_true", help="工作树有未提交改动也照样派")
    ap.add_argument("--allow-noop", action="store_true", help="一个文件都没改也算成功")
    ap.add_argument("worktree")
    ap.add_argument("task", help="任务文字；@文件 或 - 读标准输入")
    a = ap.parse_args(argv)
    c = cfg()
    wt = Path(a.worktree).expanduser().resolve()
    if not wt.is_dir():
        die(f"目录不存在：{wt}")
    if not is_git(wt):
        die(f"{wt} 不是 git 仓库。先 git init 并提交一次，交回后才看得清它改了什么。")
    if git(wt, "status", "--porcelain").strip() and not a.allow_dirty and not a.resume:
        die("工作树有未提交改动，交回后分不清哪些是 Codex 改的。先提交，或加 --allow-dirty。")
    task = read_task(a.task)
    sensitive_guard(collect_guard_texts([task, str(wt)], target_dir=wt, extra_files=task_paths(task), include_dir_files=True),
                    a.allow_sensitive, "OpenAI（GPT）")

    models: list[str | None] = [a.model] if a.model else (as_list(c.get("codex_models")) or [None])
    effort = a.effort or c["codex_effort"]
    job = new_job("codex-worker", task)
    base = git(wt, "rev-parse", "HEAD").strip() if has_git_head(wt) else "unborn"
    before = fingerprint(wt)
    t0 = time.time()
    used, code, out, errs = models[0], 1, "", []
    for m in models:
        used = m
        common = [*model_args(m), "-c", f"model_reasoning_effort={effort}",
                  "-c", 'sandbox_mode="workspace-write"', *as_list(c.get("codex_extra_args")),
                  "--json", "-o", str(job / "output.md")]
        if a.resume:
            cmd = ["codex", "exec", "resume", a.resume, *common, task]
        else:
            cmd = ["codex", "exec", *common, "-C", str(wt), "--skip-git-repo-check", f"{WORKER_RULES}\n\n【任务】\n{task}"]
        code, out, err = run(cmd, cwd=wt, timeout=a.timeout)
        if err.strip():
            errs.append(f"=== [{m} exit={code}] ===\n{err.strip()}")
        if code != 0 and _is_model_unsupported(out + err) and m is not models[-1]:
            print(f"[md] 模型 {m} 当前账号不支持，换下一个", file=sys.stderr)
            continue
        break

    (job / "events.jsonl").write_text(out)
    (job / "stderr.txt").write_text("\n\n".join(errs) + "\n")
    sid = a.resume
    if (m_sid := re.search(r'"thread_id":"([^"]+)"', out)):
        sid = m_sid.group(1)
    report = (job / "output.md").read_text() if (job / "output.md").exists() else ""
    changed = fingerprint(wt) != before
    has_report = code == 0 and bool(report.strip())
    ok = has_report and (changed or a.allow_noop)
    log_job(job, tool="codex-worker", model=used, effort=effort, worktree=str(wt), base=base,
            session=sid, ok=ok, worktree_changed=changed, seconds=round(time.time() - t0))
    if not has_report:
        die(f"Codex 没交回结果（退出码 {code}）。{_codex_errors(chr(10).join(errs), out)}\n记录：{job}", code=1)
    if not changed and not a.allow_noop:
        print(report.strip())
        die(f"Codex 交了报告，但工作树一个文件都没变（本来就不用改文件的话加 --allow-noop）。\n会话ID：{sid}  记录：{job}", code=4)

    print(report.strip())
    print(f"\n--- 工作树变化（基线 {base[:8]}）---")
    print(git(wt, "status", "--short").strip() or "（没有变化）")
    if (stat := git(wt, "diff", "--stat")).strip():
        print(stat.rstrip())
    if (unt := _untracked_summary(wt)).strip():
        print(unt.rstrip())
    print(f"\n[md] 模型 {used or '默认'}（{effort}），会话ID {sid}（追加：codex-worker --resume {sid} {wt} \"追加\"）", file=sys.stderr)
    print(f"[md] 用时 {round(time.time() - t0)} 秒，记录 {job}。下一步：读 diff、自己跑测试。", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- codex-review

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "blocking", "suggestions"],
    "properties": {
        "summary": {"type": "string"},
        "blocking": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["file", "line", "problem", "how_it_happens", "fix"],
            "properties": {
                "file": {"type": "string"}, "line": {"type": "integer"},
                "problem": {"type": "string"}, "how_it_happens": {"type": "string"},
                "fix": {"type": "string"},
            }}},
        "suggestions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["file", "line", "text"],
            "properties": {"file": {"type": "string"}, "line": {"type": "integer"},
                           "text": {"type": "string"}}}},
    },
}

REVIEW_RULES = """你是代码审查员。审查下面这次改动（diff 在最后；需要上下文就读仓库里的文件，不要改任何文件）。
问题分两级：
【挡上线 blocking】只有以下情况，并且你能写清楚「在什么输入 / 状态下、一步步怎么发生」才算：
  算错钱或数量、丢失或写坏数据、越权或泄露密钥 / 个人信息、发错消息或对外误发、让发布或定时任务出事故（崩溃、死循环、跑不起来）。
  需要好几件事巧合同时发生才会出现的，不算挡上线，放进建议。
【建议 suggestions】其他所有问题：可读性、边界情况、风格、小优化。最多 8 条，挑最值得的。
summary 用中文两三句话，给不懂代码的人看：这次改动做了什么、能不能上。
所有文字用中文。"""


def detect_sensitive_changes(diff: str, patterns: list[str]) -> list[str]:
    """检查改动的文件路径和增删行内容是否命中高风险词（钱、登录权限、对外发消息）。"""
    if not patterns:
        return []
    pat = re.compile("|".join(patterns), re.I)
    files: list[str] = []
    for m in re.finditer(r"^(?:\+\+\+ b/|--- a/)(.+)$", diff, re.M):
        f = m.group(1).strip().strip('"')
        if f != "/dev/null" and f not in files:
            files.append(f)
    hits = [f for f in files if pat.search(f)]
    if hits:
        return hits
    for line in diff.splitlines():
        if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---")):
            if (m := pat.search(line[1:])):
                return [f"diff内容({m.group(0)})"]
    return []


def compute_review_round(repo: Path, branch: str, joblog_path: Path | None = None) -> int:
    """第几轮审查：倒序看同仓库同分支 6 小时内的成功审查；上一次通过就归零；运行失败不算轮次。"""
    log_file = joblog_path or JOBLOG
    if not log_file.is_file():
        return 1
    cutoff = time.time() - 6 * 3600
    rows = []
    for line in log_file.read_text(errors="ignore").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    failed = 0
    for j in reversed(rows):
        if j.get("tool") != "codex-review" or j.get("repo") != str(repo) or j.get("branch") != branch:
            continue
        try:
            if dt.datetime.fromisoformat(j["time"]).timestamp() <= cutoff:
                break
        except Exception:
            continue
        if not j.get("ok"):
            continue
        if j.get("blocking", 0) == 0:
            break
        failed += 1
    return failed + 1


def parse_review(raw: str) -> dict | None:
    """读审查结果 JSON（容忍外面包了 ```json 代码块）。字段不全就返回 None。"""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        res = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(res, dict) or not isinstance(res.get("blocking"), list) or not isinstance(res.get("suggestions"), list):
        return None
    res.setdefault("summary", "")
    for b in res["blocking"]:
        for k in ("file", "problem", "how_it_happens", "fix"):
            b.setdefault(k, "")
        b.setdefault("line", 0)
    for x in res["suggestions"]:
        x.setdefault("file", ""); x.setdefault("line", 0); x.setdefault("text", "")
    return res


def cmd_codex_review(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="codex-review", description="GPT 代码审查：挡上线 / 建议两级。")
    tgt = ap.add_mutually_exclusive_group()
    tgt.add_argument("--base", help="和这个分支 / 提交比较（默认：未提交改动；没有就审上一个提交）")
    tgt.add_argument("--commit", help="只审这一个提交")
    ap.add_argument("--deep", action="store_true", help="强制用深档推理")
    ap.add_argument("--model", help="指定模型（默认按 codex_models 顺序尝试）")
    ap.add_argument("--allow-sensitive", action="store_true")
    ap.add_argument("--no-fallback", action="store_true", help="Codex 不可用时不让快模型替补审查")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("repo", nargs="?", default=".")
    a = ap.parse_args(argv)
    c = cfg()
    repo = Path(a.repo).expanduser().resolve()
    if not is_git(repo):
        die(f"{repo} 不是 git 仓库")
    if a.commit:
        diff, target = git(repo, "show", "--format=%H %s", a.commit), f"commit {a.commit}"
    elif a.base:
        diff, target = git(repo, "diff", f"{a.base}...HEAD"), f"{a.base}...HEAD"
    else:
        diff, target = working_tree_diff(repo), "未提交改动"
        if not diff.strip() and has_git_head(repo):
            diff, target = git(repo, "show", "--format=%H %s", "HEAD"), "最近一个提交"
    if not diff.strip():
        die("没有可审查的改动")
    if len(diff) > 400_000:
        die(f"diff 太大（{len(diff)} 字符），请拆小或用 --commit 分批审")
    sensitive_guard(collect_guard_texts([diff, str(repo)], target_dir=repo), a.allow_sensitive, "OpenAI（GPT）")

    risky = detect_sensitive_changes(diff, as_list(c.get("deep_review_patterns")))
    effort = c["review_deep_effort"] if (a.deep or risky) else c["review_effort"]
    branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() or "HEAD"
    rnd = compute_review_round(repo, branch)

    job = new_job("codex-review", f"{repo.name} {target}")
    schema = job / "schema.json"
    schema.write_text(json.dumps(REVIEW_SCHEMA))
    prompt = f"{REVIEW_RULES}\n\n审查范围：{target}\n\n```diff\n{diff}\n```"
    models: list[str | None] = [a.model] if a.model else (as_list(c.get("codex_models")) or [None])
    t0 = time.time()
    used, code, errs, out = models[0], 1, [], ""
    for m in models:
        used = m
        code, out, err = run(
            ["codex", "exec", *model_args(m), "-c", f"model_reasoning_effort={effort}",
             "-s", "read-only", *as_list(c.get("codex_extra_args")),
             "-C", str(repo), "--skip-git-repo-check",
             "--output-schema", str(schema), "-o", str(job / "result.json"), "-"],
            cwd=repo, timeout=a.timeout, stdin_text=prompt,
        )
        if err.strip():
            errs.append(f"=== [{m} exit={code}] ===\n{err.strip()}")
        if code != 0 and _is_model_unsupported(out + err) and m is not models[-1]:
            print(f"[md] 模型 {m} 当前账号不支持，换下一个", file=sys.stderr)
            continue
        break
    res = parse_review((job / "result.json").read_text() if (job / "result.json").exists() else "")
    fell_back = False
    if res is None and c.get("fallback_to_fast") and not a.no_fallback and fast_key(c):
        why = _codex_errors(chr(10).join(errs), out).splitlines()
        print(f"[md] Codex 审查没出结果（{why[0][:160] if why else f'退出码 {code}'}），改由快模型替补审查（它只看得到 diff）", file=sys.stderr)
        try:
            raw = fast_chat(c, f"{prompt}\n\n只输出一个 JSON 对象，符合这个结构，不要别的文字：\n{json.dumps(REVIEW_SCHEMA)}", a.timeout)
            res = parse_review(raw)
            if res is not None:
                used, effort, fell_back = c["fast_model"], "替补", True
            else:
                errs.append(f"=== [fallback] 返回不是合格 JSON ===\n{raw[:1500]}")
        except Exception as e:
            errs.append(f"=== [fallback] ===\n{e}")
    (job / "stderr.txt").write_text("\n\n".join(errs) + "\n")
    if res is None:
        log_job(job, tool="codex-review", model=used, repo=str(repo), branch=branch, round=rnd, ok=False)
        die(f"审查没返回可读结果（退出码 {code}）。{_codex_errors(chr(10).join(errs), out)}\n记录：{job}", code=1)
    nb = len(res["blocking"])
    log_job(job, tool="codex-review", model=used, repo=str(repo), branch=branch, round=rnd, effort=effort,
            target=target, ok=True, blocking=nb, suggestions=len(res["suggestions"]), fallback=fell_back,
            seconds=round(time.time() - t0))

    note = f"，命中高风险项：{', '.join(risky[:3])}" if risky else ""
    lines = [f"# 代码审查（第 {rnd} 轮，{target}，模型 {used or '默认'}，推理 {effort}{note}）", "", res["summary"], ""]
    if fell_back:
        lines += ["> 注意：Codex 不可用，本次由快模型替补审查，它只看得到 diff。挡上线条目请自己核实。", ""]
    lines.append(f"## 挡上线：{nb} 条" if nb else "## 挡上线：无")
    for i, b in enumerate(res["blocking"], 1):
        lines += [f"{i}. `{b['file']}:{b['line']}` {b['problem']}",
                  f"   - 怎么发生：{b['how_it_happens']}", f"   - 怎么改：{b['fix']}"]
    if res["suggestions"]:
        lines += ["", f"## 建议：{len(res['suggestions'])} 条（不挡上线，记下即可）"]
        lines += [f"- `{s['file']}:{s['line']}` {s['text']}" for s in res["suggestions"]]
    lines.append("")
    if nb == 0:
        lines.append("结论：可以上线。")
    elif rnd >= 2:
        lines.append("结论：已审两轮仍有挡上线问题。停下来，把这些问题和你的判断交给用户决定：继续修还是允许上线。")
    else:
        lines.append("结论：修完挡上线的问题后再审一轮（最多两轮）。")
    text = "\n".join(lines)
    (job / "review.md").write_text(text)
    print(text)
    print(f"\n[md] 用时 {round(time.time() - t0)} 秒，记录 {job}", file=sys.stderr)
    return 0 if nb == 0 else 10


# ---------------------------------------------------------------- md-jobs

def cmd_jobs(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="md-jobs", description="最近派出去的活")
    ap.add_argument("-n", type=int, default=15)
    a = ap.parse_args(argv)
    if not JOBLOG.exists():
        print("还没有派过活")
        return 0
    rows = [json.loads(l) for l in JOBLOG.read_text().splitlines() if l.strip()][-a.n:]
    for j in rows:
        extra = j.get("model") or j.get("mode") or ""
        if j.get("tool") == "codex-review":
            extra = f"{j.get('model') or ''} 第{j.get('round')}轮 挡{j.get('blocking', '?')} 建议{j.get('suggestions', '?')}".strip()
        if j.get("fallback"):
            extra += "（替补）"
        print(f"{j['time'][5:16]}  {'✓' if j.get('ok') else '✗'}  {j.get('tool', ''):<13} "
              f"{str(j.get('seconds', '')):>5}s  {extra}  {Path(j['job']).name}")
    return 0


COMMANDS = {
    "gemini-ask": cmd_gemini_ask,
    "codex-worker": cmd_codex_worker,
    "codex-review": cmd_codex_review,
    "fast-ask": cmd_fast_ask,
    "md-jobs": cmd_jobs,
}


def main() -> int:
    name = Path(sys.argv[0]).name
    if name in COMMANDS:
        return COMMANDS[name](sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] in COMMANDS:
        return COMMANDS[sys.argv[1]](sys.argv[2:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
