# model-dispatch: Claude as the lead, delegating to Gemini, GPT and DeepSeek

[中文](README.md)

A Claude Code skill. You talk only to Claude. Claude breaks the work down, delegates it, and verifies the results:

- **Gemini** is a read-only advisor: independent plans, copy edits, critique.
- **GPT (Codex)** writes well-specified code modules and reviews code before release.
- **DeepSeek** (or any OpenAI-compatible fast model) handles text jobs that finish in seconds.

Inspired by a WeChat article by 数字生命卡兹克 (Khazix) on making Claude, GPT and Gemini work together.

## What the scripts guard against

- A Gemini model is out of quota → try the next model; if all fail, a fast model fills in and the output says so.
- The read-only advisor changes files → a directory fingerprint before and after triggers a warning.
- Codex reports success but changed nothing → exit code 4.
- You cannot tell what Codex changed → a clean git tree is required; changes and the session ID are printed.
- Too many review comments → findings are split into "blocking" and "suggestions".
- Changes touch money, auth or outgoing messages → review uses the deep reasoning tier.
- Endless fix/review loops → rounds are counted; after round 2 the decision goes back to you.
- Codex is down → the fast model reviews the diff instead, flagged as a fallback.
- Client data sent by accident → configurable `sensitive_markers` block the call (exit code 3).
- Every job is logged; `md-jobs` lists them.

## Install

Requires Python 3.10+, git, Claude Code. Per channel: Gemini CLI (`gemini`), Codex CLI (`codex`), and `DEEPSEEK_API_KEY` for the fast model.

```bash
git clone https://github.com/<you>/model-dispatch.git ~/model-dispatch && ~/model-dispatch/install.sh
```

The installer links 5 commands into `~/.local/bin`, links the skill into `~/.claude/skills/`, and imports `rules/CLAUDE.md` from `~/.claude/CLAUDE.md`.

## Commands

| Command | Purpose |
|---|---|
| `gemini-ask [--plan\|--copy\|--review] <dir> "task"` | Gemini read-only advisor |
| `codex-worker [--resume ID] <git dir> "task"` | Codex writes code in a workspace sandbox |
| `codex-review [--base B\|--commit SHA] [repo]` | Two-level code review |
| `fast-ask "task"` | Fast text-only job |
| `md-jobs` | Recent jobs |

Prompts and messages are in Chinese; edit `scripts/md.py` and `rules/CLAUDE.md` to localize. Config: `~/.config/model-dispatch/config.json` (keys match `DEFAULTS` in `scripts/md.py`). Tests: `python3 scripts/test_md.py` (offline, ~2 s).

## License

MIT
