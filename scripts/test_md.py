#!/usr/bin/env python3
"""model-dispatch 离线守卫测试。不联网、不调任何模型，约 2 秒。

python3 scripts/test_md.py

每一条都对应一个真实踩过的坑：
1. 审查生成 diff 时不能弄脏真实的 .git/index（否则 git commit -am 会把杂物提交进去）
2. 任务里点名的文件路径含空格也要认出来，且只认文件不认目录
3. 改到钱 / 登录 / 发消息的代码，审查自动升深档
4. 审查轮次：上一轮通过就归零，运行失败不算一轮
5. 敏感词拦截能拦住 git remote、目录文件名、点名文件内容；没配敏感词时不拦
6. codex-worker 一个文件都没改，默认判失败（退出码 4）
7. gemini-ask 的命令模板：没指定模型时去掉 -m；模板里没 {prompt} 就走标准输入
8. gemini-ask 某个模型额度用完，自动换下一个
9. 审查结果包在 ```json 里也能读；字段不全判失败
10. Codex 额度用完的报错在标准输出的 JSON 事件里，失败信息要把它说出来
11. 敏感词藏在目录里某个文件的正文里（文件名不含、甚至被 gitignore），也要拦住
"""
from __future__ import annotations

import datetime as dt
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import md  # noqa: E402


def init_repo(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=p, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=p, check=True)


def commit_all(p: Path, msg: str = "init") -> None:
    subprocess.run(["git", "add", "-A"], cwd=p, check=True)
    subprocess.run(["git", "commit", "-qm", msg], cwd=p, check=True)


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="md-ut-")
        self.root = Path(self._tmp.name)
        self._orig = (md.DATA, md.JOBS, md.JOBLOG, md.CONFIG)
        md.DATA = self.root / "data"
        md.JOBS = md.DATA / "jobs"
        md.JOBLOG = md.DATA / "jobs.jsonl"
        md.CONFIG = self.root / "config.json"

    def tearDown(self) -> None:
        md.DATA, md.JOBS, md.JOBLOG, md.CONFIG = self._orig
        self._tmp.cleanup()

    def write_cfg(self, **kw) -> None:
        md.CONFIG.write_text(json.dumps(kw))

    def test_working_tree_diff_keeps_real_index(self) -> None:
        repo = self.root / "r"
        init_repo(repo)
        (repo / "a.txt").write_text("1\n")
        commit_all(repo)
        (repo / "new.txt").write_text("hello\n")
        before = subprocess.run(["git", "status", "--porcelain"], cwd=repo, text=True, capture_output=True).stdout
        diff = md.working_tree_diff(repo)
        after = subprocess.run(["git", "status", "--porcelain"], cwd=repo, text=True, capture_output=True).stdout
        self.assertIn("new.txt", diff)
        self.assertEqual(before, after, "真实 .git/index 被改了")

    def test_task_paths_with_spaces(self) -> None:
        d = self.root / "Application Support"
        d.mkdir()
        f = d / "notes.md"
        f.write_text("x")
        task = f"请看 `{f}` 和 {f} ，目录 {d} 不算"
        self.assertEqual(md.task_paths(task), [str(f)])

    def test_detect_sensitive_changes(self) -> None:
        self.assertEqual(md.detect_sensitive_changes("+++ b/src/payment.py\n+x=1\n", [r"pay"]), ["src/payment.py"])
        hit = md.detect_sensitive_changes("+++ b/util.py\n+send_webhook(url)\n", [r"webhook"])
        self.assertTrue(hit and hit[0].startswith("diff内容"))
        self.assertEqual(md.detect_sensitive_changes("+++ b/util.py\n+x=1\n", [r"pay"]), [])

    def test_review_round_resets_after_pass(self) -> None:
        now = dt.datetime.now().isoformat(timespec="seconds")
        rows = [
            {"tool": "codex-review", "repo": "/r", "branch": "main", "ok": True, "blocking": 2, "time": now},
            {"tool": "codex-review", "repo": "/r", "branch": "main", "ok": True, "blocking": 0, "time": now},
            {"tool": "codex-review", "repo": "/r", "branch": "main", "ok": True, "blocking": 1, "time": now},
            {"tool": "codex-review", "repo": "/r", "branch": "main", "ok": False, "time": now},
        ]
        md.DATA.mkdir(parents=True)
        md.JOBLOG.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        self.assertEqual(md.compute_review_round(Path("/r"), "main"), 2)
        self.assertEqual(md.compute_review_round(Path("/r"), "dev"), 1)

    def test_sensitive_guard(self) -> None:
        repo = self.root / "proj"
        init_repo(repo)
        subprocess.run(["git", "remote", "add", "origin", "git@example.com:acme-corp/x.git"], cwd=repo, check=True)
        (repo / "readme.md").write_text("hi")
        named = self.root / "brief.md"
        named.write_text("客户：Globex")
        # 没配敏感词：不拦
        md.sensitive_guard(md.collect_guard_texts(["随便"], target_dir=repo, include_dir_files=True), False, "X")
        self.write_cfg(sensitive_markers=["acme-corp", "globex"])
        for texts in (
            md.collect_guard_texts(["随便"], target_dir=repo),                    # git remote
            md.collect_guard_texts(["随便"], extra_files=[str(named)]),           # 点名文件内容
        ):
            with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
                md.sensitive_guard(texts, False, "X")
            self.assertEqual(cm.exception.code, 3)
        md.sensitive_guard(md.collect_guard_texts(["随便"], target_dir=repo), True, "X")  # 放行

    def test_codex_worker_noop_rejected(self) -> None:
        repo = self.root / "w"
        init_repo(repo)
        (repo / "a.txt").write_text("1\n")
        commit_all(repo)

        def fake_run(cmd, cwd=None, timeout=None, stdin_text=None):
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_text("看过了，没改")
            return 0, '{"thread_id":"t1"}', ""

        with mock.patch.object(md, "run", fake_run), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                md.cmd_codex_worker([str(repo), "改点东西"])
            self.assertEqual(cm.exception.code, 4)
            self.assertEqual(md.cmd_codex_worker(["--allow-noop", str(repo), "改点东西"]), 0)

    def test_build_cmd(self) -> None:
        t = ["gemini", "-m", "{model}", "-p", "{prompt}"]
        self.assertEqual(md.build_cmd(t, "hi", None), (["gemini", "-p", "hi"], None))
        self.assertEqual(md.build_cmd(t, "hi", "g2"), (["gemini", "-m", "g2", "-p", "hi"], None))
        self.assertEqual(md.build_cmd(["x", "--model={model}"], "hi", None), (["x"], "hi"))

    def test_gemini_ask_switches_model_on_quota(self) -> None:
        d = self.root / "dir"
        d.mkdir()
        self.write_cfg(gemini_models=["pro", "flash"], fallback_to_fast=False)
        seen = []

        def fake_run(cmd, cwd=None, timeout=None, stdin_text=None):
            seen.append(cmd[cmd.index("-m") + 1])
            if seen[-1] == "pro":
                return 1, "", "429 RESOURCE_EXHAUSTED"
            return 0, "结论：可以。", ""

        buf = io.StringIO()
        with mock.patch.object(md, "run", fake_run), redirect_stdout(buf), redirect_stderr(io.StringIO()):
            self.assertEqual(md.cmd_gemini_ask([str(d), "问个问题"]), 0)
        self.assertEqual(seen, ["pro", "flash"])
        self.assertIn("可以", buf.getvalue())

    def test_parse_review(self) -> None:
        ok = md.parse_review('```json\n{"summary":"s","blocking":[{"file":"a","line":1,"problem":"p"}],"suggestions":[]}\n```')
        self.assertEqual(ok["blocking"][0]["fix"], "")
        self.assertIsNone(md.parse_review('{"summary":"s"}'))
        self.assertIsNone(md.parse_review("没有 JSON"))

    def test_codex_errors_reads_json_events(self) -> None:
        out = '{"type":"turn.started"}\n{"type":"error","message":"You have hit your usage limit."}\n'
        self.assertIn("usage limit", md._codex_errors("Reading additional input from stdin...", out))

    def test_sensitive_in_dir_file_content(self) -> None:
        repo = self.root / "proj2"
        init_repo(repo)
        (repo / "docs").mkdir()
        (repo / "docs" / "会议.md").write_text("今天和 Globex 开会")
        (repo / ".gitignore").write_text("secret.env\n")
        (repo / "secret.env").write_text("TOKEN_OWNER=initech")
        (repo / "logo.png").write_bytes(b"\x89PNG\x00\x00globex")
        self.write_cfg(sensitive_markers=["globex"])
        with self.assertRaises(SystemExit) as cm, redirect_stderr(io.StringIO()):
            md.sensitive_guard(md.collect_guard_texts(["按 docs/会议.md 整理需求"], target_dir=repo, include_dir_files=True), False, "X")
        self.assertEqual(cm.exception.code, 3)
        self.write_cfg(sensitive_markers=["initech"])  # gitignore 掉的文件模型也读得到
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            md.sensitive_guard(md.collect_guard_texts(["x"], target_dir=repo, include_dir_files=True), False, "X")
        (repo / "docs" / "会议.md").write_text("普通内容")
        self.write_cfg(sensitive_markers=["globex"])  # 只在二进制里出现：不拦
        md.sensitive_guard(md.collect_guard_texts(["x"], target_dir=repo, include_dir_files=True), False, "X")


if __name__ == "__main__":
    unittest.main(verbosity=1)
