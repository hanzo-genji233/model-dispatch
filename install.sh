#!/usr/bin/env bash
# 安装 model-dispatch：命令软链到 ~/.local/bin，skill 软链到 ~/.claude/skills，规则接进 ~/.claude/CLAUDE.md。
# 可重复运行。卸载：删掉这些软链，再删 CLAUDE.md 里带 model-dispatch 的那一行。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="${BIN_DIR:-$HOME/.local/bin}"
mkdir -p "$BIN" "$HOME/.claude/skills"
chmod +x "$HERE/scripts/md.py"
for c in gemini-ask codex-worker codex-review fast-ask md-jobs; do
  ln -sfn "$HERE/scripts/md.py" "$BIN/$c"
done
ln -sfn "$HERE" "$HOME/.claude/skills/model-dispatch"
LINE="@$HERE/rules/CLAUDE.md"
touch "$HOME/.claude/CLAUDE.md"
grep -qF "$LINE" "$HOME/.claude/CLAUDE.md" || printf '\n# 多模型派活（model-dispatch）\n%s\n' "$LINE" >> "$HOME/.claude/CLAUDE.md"
mkdir -p "$HOME/.config/model-dispatch"
[ -f "$HOME/.config/model-dispatch/config.json" ] || cp "$HERE/config.example.json" "$HOME/.config/model-dispatch/config.json"

echo "已安装。体检："
for t in python3 git codex gemini; do
  if command -v "$t" >/dev/null; then echo "  ✓ $t"; else echo "  ✗ $t 没装（对应通道用不了）"; fi
done
case ":$PATH:" in *":$BIN:"*) ;; *) echo "  ! $BIN 不在 PATH 里，请加进去";; esac
if [ -n "${FAST_API_KEY:-}${DEEPSEEK_API_KEY:-}" ]; then echo "  ✓ 快模型 key"; else echo "  ✗ 没设 DEEPSEEK_API_KEY（fast-ask 和替补用不了）"; fi
echo "配置：~/.config/model-dispatch/config.json"
