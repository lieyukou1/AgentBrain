import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brain import (  # noqa: E402
    brief, db, enrich, funnel, gitutil, ingest, link, mine, probes, redact, refine, replay,
    rules, search, verify,
)


class BrainTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["BRAIN_HOME"] = self.tmp.name
        (Path(self.tmp.name) / "probes").mkdir(parents=True)
        for p in (ROOT / "probes").glob("*.json"):
            shutil.copy(p, Path(self.tmp.name) / "probes" / p.name)
        self.conn = db.connect(Path(self.tmp.name) / "data" / "brain.db")
        self.addCleanup(self.conn.close)


class AtomTests(BrainTestBase):
    def test_atom_roundtrip_and_rate_requires_resolved_anchor(self):
        aid = db.add_atom(self.conn, {"claim": "测试用陈述", "repo": "/nonexistent"})
        db.add_anchor(self.conn, aid, "file", path="a.py", excerpt="x")
        row = db.get_atom(self.conn, aid)
        self.assertEqual(row["status"], "candidate")
        self.assertEqual(len(db.anchors_of(self.conn, aid)), 1)

    def test_outcome_updates_confidence(self):
        aid = db.add_atom(self.conn, {"claim": "c", "confidence": 0.5})
        db.record_outcome(self.conn, aid, "validated")
        self.assertAlmostEqual(db.get_atom(self.conn, aid)["confidence"], 0.65, places=2)
        db.record_outcome(self.conn, aid, "insufficient")
        self.assertAlmostEqual(db.get_atom(self.conn, aid)["confidence"], 0.45, places=2)


def _make_diff(path: str, lines: list[str]) -> str:
    """把若干新增行包成最小 unified diff，供探针用例使用。"""
    body = "".join(f"+{line}\n" for line in lines)
    return f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,{len(lines)} @@\n{body}"


class ProbeTests(BrainTestBase):
    def test_http_without_timeout_fires(self):
        diff = (
            "diff --git a/src/pay.py b/src/pay.py\n"
            "--- a/src/pay.py\n"
            "+++ b/src/pay.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def pay():\n"
            "+    return requests.post('https://api.example.com', json={})\n"
        )
        result = probes.run_check(self.conn, diff)
        ids = [f["probe"] for f in result["findings"]]
        self.assertIn("http-without-timeout", ids)

    def test_http_with_timeout_does_not_fire(self):
        diff = (
            "--- a/src/pay.py\n"
            "+++ b/src/pay.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def pay():\n"
            "+    return requests.post(url, timeout=3)\n"
        )
        result = probes.run_check(self.conn, diff)
        ids = [f["probe"] for f in result["findings"]]
        self.assertNotIn("http-without-timeout", ids)

    def test_parse_diff_line_numbers(self):
        diff = "--- a/x\n+++ b/x\n@@ -1,2 +10,3 @@\n context\n+added\n more\n"
        files = probes.parse_diff(diff)
        self.assertEqual(files["x"], [(11, "added")])

    def test_test_files_are_skipped(self):
        diff = ("--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -0,0 +1,2 @@\n"
                "+import requests\n+requests.post(url)\n")
        self.assertEqual(probes.run_check(self.conn, diff)["findings"], [])

    def test_docstring_and_prose_are_skipped(self):
        diff = ('--- a/src/a.py\n+++ b/src/a.py\n@@ -0,0 +1,5 @@\n'
                '+"""\n'
                '+writes beyond the thread upsert that authorization gates.\n'
                '+"""\n'
                '+self.message_sessions = self.db["message_sessions"]\n'
                '+lock_registry = {"a": 1}\n')
        findings = probes.run_check(self.conn, diff)["findings"]
        ids = [f["probe"] for f in findings]
        self.assertNotIn("write-without-idempotency", ids)
        self.assertNotIn("concurrency-without-sync", ids)

    def test_related_uses_distinctive_stem_not_shared_folder(self):
        from brain import search
        lease = db.add_atom(self.conn, {"claim": "租约续期时若只用 modified_count 判定会误报", "confidence": 0.5})
        db.add_anchor(self.conn, lease, "file", path="src/repo/lease_manager.py")
        noise = db.add_atom(self.conn, {"claim": "候选人不可用时不应返回 403", "confidence": 0.9})
        db.add_anchor(self.conn, noise, "file", path="src/conversation/views.py")

        hits = search.related(self.conn, "src/conversation/request_lease.py", limit=2,
                              hint="租约|lease")
        ids = [h["id"] for h in hits]
        self.assertIn(lease, ids)
        self.assertNotIn(noise, ids)

    def test_specific_stem_outranks_bare_same_folder(self):
        from brain import search
        near = db.add_atom(self.conn, {"claim": "心跳任务未在停止时释放会导致资源泄漏", "confidence": 0.9})
        db.add_anchor(self.conn, near, "file", path="src/conversation/heartbeat.py")
        far = db.add_atom(self.conn, {"claim": "lease_manager 中刷新间隔过大导致误判", "confidence": 0.4})
        db.add_anchor(self.conn, far, "file", path="src/repo/lease_manager.py")

        hits = search.related(self.conn, "src/conversation/request_lease.py", limit=2,
                              hint="心跳|lease")
        # 词干级证据（lease）比"只是同目录"更有力，即使后者置信度更高
        self.assertEqual(hits[0]["id"], far)

    def test_require_any_blocks_retry_without_io_context(self):
        no_io = ("--- a/src/a.py\n+++ b/src/a.py\n@@ -0,0 +1,3 @@\n"
                 "+for i in range(3):\n"
                 "+    value = parse(text)\n"
                 "+    if value: break  # 重试\n")
        self.assertNotIn("retry-without-backoff",
                         [f["probe"] for f in probes.run_check(self.conn, no_io)["findings"]])
        with_io = ("--- a/src/b.py\n+++ b/src/b.py\n@@ -0,0 +1,3 @@\n"
                   "+for i in range(3):\n"
                   "+    resp = client.get(url)  # 重试\n"
                   "+    if resp: break\n")
        self.assertIn("retry-without-backoff",
                      [f["probe"] for f in probes.run_check(self.conn, with_io)["findings"]])

    def test_require_any_blocks_write_without_persistence_context(self):
        # 列表 .insert() 不是持久化写入，不该报幂等（.add() 压根不匹配 added_any，测不到门禁）
        memory = ("--- a/src/a.py\n+++ b/src/a.py\n@@ -0,0 +1,3 @@\n"
                  "+def prepend(items, newest):\n"
                  "+    items.insert(0, newest)\n"
                  "+    return items\n")
        self.assertNotIn("write-without-idempotency",
                         [f["probe"] for f in probes.run_check(self.conn, memory)["findings"]])
        db_write = ("--- a/src/b.py\n+++ b/src/b.py\n@@ -0,0 +1,2 @@\n"
                    "+def save(db, row):\n"
                    "+    return db.collection.insert(row)\n")
        self.assertIn("write-without-idempotency",
                      [f["probe"] for f in probes.run_check(self.conn, db_write)["findings"]])

    def test_require_any_blocks_http_without_request_target(self):
        # 只构造客户端、没有任何请求目标时证据不足，不触发
        only_client = ("--- a/src/a.py\n+++ b/src/a.py\n@@ -0,0 +1,1 @@\n"
                       "+client = HttpClient.newHttpClient()\n")
        self.assertNotIn("http-without-timeout",
                         [f["probe"] for f in probes.run_check(self.conn, only_client)["findings"]])
        with_url = ("--- a/src/b.py\n+++ b/src/b.py\n@@ -0,0 +1,1 @@\n"
                    "+resp = fetch('https://api.example.com/v1/orders')\n")
        self.assertIn("http-without-timeout",
                      [f["probe"] for f in probes.run_check(self.conn, with_url)["findings"]])

    def test_no_finding_still_returns_related_atoms(self):
        """没有命中时也要给出 atom_id，否则 record_outcome 永远没有可评对象。"""
        aid = db.add_atom(self.conn, {"claim": "回看候选要按时间窗收口"})
        db.add_anchor(self.conn, aid, "file", path="src/pipeline/recall.py")
        diff = ("--- /dev/null\n+++ b/src/pipeline/recall.py\n@@ -0,0 +1,2 @@\n"
                "+\"\"\"doc\"\"\"\n+X = 1\n")
        result = probes.run_check(self.conn, diff)
        self.assertEqual(result["findings"], [])
        self.assertEqual([a["id"] for a in result["related_atoms"]], [aid])

    def test_findings_suppress_related_atoms(self):
        """有命中时经验挂在 finding 上，不重复给一份。"""
        diff = ("--- a/src/a.py\n+++ b/src/a.py\n@@ -0,0 +1,1 @@\n"
                "+resp = fetch('https://api.example.com/v1/x')\n")
        result = probes.run_check(self.conn, diff)
        self.assertTrue(result["findings"])
        self.assertEqual(result["related_atoms"], [])

    def test_compiled_probes_fire_on_their_patterns(self):
        """反向编译的四条探针：对着经验降维出来的写法必须能触发。"""
        cases = {
            "update-count-as-outcome": [
                "result = await coll.update_one({'_id': id}, {'$set': {'owner': o}})",
                "if result.modified_count == 0:",
                "    raise LeaseLost()",
            ],
            "llm-json-without-raw-decode": [
                "content = response.choices[0].message.content",
                "data = json.loads(content)",
            ],
            "model-score-to-int-without-finite-check": [
                "score = int(round(float(payload.get('score', 0))))",
            ],
            "http-status-only-success": [
                "resp = requests.post(url, json=body)",
                "if resp.status_code == 200:",
            ],
            "broad-except-drops-detail": [
                "except Exception:",
                "    return None",
            ],
            "max-tokens-too-small-for-reasoning": [
                "resp = client.chat(model='deepseek-reasoner', max_tokens=400)",
            ],
            "schema-strict-disabled": [
                "tool = convert_to_openai_tool(fn, strict=False)",
            ],
            "retry-exhaustion-drops-envelope": [
                "for attempt in range(max_retries):",
                "    last_error = None",
            ],
        }
        for probe_id, lines in cases.items():
            with self.subTest(probe=probe_id):
                diff = _make_diff("src/a.py", lines)
                found = [f["probe"] for f in probes.run_check(self.conn, diff)["findings"]]
                self.assertIn(probe_id, found)

    def test_compiled_probes_respect_unless_guard(self):
        """已经有防护写法时不该再报，否则规则会退化成噪声。"""
        cases = {
            "update-count-as-outcome": [
                "if result.modified_count == 0 and result.matched_count == 0:",
            ],
            "llm-json-without-raw-decode": [
                "content = response.choices[0].message.content",
                "data, _ = json.JSONDecoder().raw_decode(content)",
            ],
            "model-score-to-int-without-finite-check": [
                "score = int(round(raw)) if math.isfinite(raw) else 0",
            ],
            "http-status-only-success": [
                "resp = requests.post(url, json=body)",
                "if resp.status_code == 200 and resp.json()['code'] == 0:",
            ],
            "broad-except-drops-detail": [
                "except Exception as exc:",
                "    logger.error('call failed', exc_info=exc)",
            ],
            "max-tokens-too-small-for-reasoning": [
                "resp = client.chat(model='deepseek-reasoner', max_tokens=8000)",
            ],
            "schema-strict-disabled": [
                "tool = convert_to_openai_tool(fn, strict=False)",
                "payload = Model.model_validate(raw)",
            ],
            "retry-exhaustion-drops-envelope": [
                "for attempt in range(max_retries):",
                "    last_retryable = resp",
            ],
        }
        for probe_id, lines in cases.items():
            with self.subTest(probe=probe_id):
                diff = _make_diff("src/a.py", lines)
                found = [f["probe"] for f in probes.run_check(self.conn, diff)["findings"]]
                self.assertNotIn(probe_id, found)

    def test_compiled_probe_reports_source_atom(self):
        """反向编译必须能溯源到经验 id，否则它和手写规则没有区别。"""
        diff = _make_diff("src/a.py", ["if result.modified_count == 0:"])
        findings = probes.run_check(self.conn, diff)["findings"]
        finding = next(f for f in findings if f["probe"] == "update-count-as-outcome")
        self.assertEqual(finding["from_atom"], "a-35ab753961b9")

    def test_doc_atom_surfaces_when_no_code_anchor_matches(self):
        """文档经验锚在 .md 上，按代码路径永远找不到；触发探针时必须靠话题词兜底。"""
        doc_atom = db.add_atom(self.conn, {
            "claim": "多实例部署下健康检查只看 HTTP 状态码会把依赖不可达的实例误判为健康",
            "status": "candidate", "derivation": "doc", "repo": "/proj"})
        db.add_anchor(self.conn, doc_atom, "file", path="docs/OPERATIONS.md",
                      excerpt="健康检查只看状态码")
        unrelated = db.add_atom(self.conn, {"claim": "候选人不可用时应返回只读历史", "status": "accepted"})
        db.add_anchor(self.conn, unrelated, "file", path="docs/API.md")
        # 代码经验即使谈到同一个词，也不该靠话题词被拖进来
        code_atom = db.add_atom(self.conn, {
            "claim": "网关返回 500 时要区分业务码与传输错误", "derivation": "llm-enriched"})

        diff = _make_diff("src/health/checker.py", [
            "resp = requests.get(url, timeout=2)",
            "if resp.status_code == 200:",
        ])
        finding = next(f for f in probes.run_check(self.conn, diff)["findings"]
                       if f["probe"] == "http-status-only-success")
        ids = [a["id"] for a in finding["atoms"]]
        self.assertIn(doc_atom, ids)
        self.assertNotIn(unrelated, ids)
        self.assertNotIn(code_atom, ids)
        self.assertEqual(finding["atoms"][0]["via"], "hint")

    def test_hint_matching_requires_whole_ascii_word(self):
        """atom_hint 里的 `ok` 不能把 token 也算命中，否则兜底关联会变成噪声源。"""
        noise = db.add_atom(self.conn, {"claim": "token 刷新失败时应重新登录"})
        diff = _make_diff("src/health/checker.py", [
            "resp = requests.get(url, timeout=2)",
            "if resp.status_code == 200:",
        ])
        finding = next(f for f in probes.run_check(self.conn, diff)["findings"]
                       if f["probe"] == "http-status-only-success")
        self.assertNotIn(noise, [a["id"] for a in finding["atoms"]])


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )


@unittest.skipIf(shutil.which("git") is None, "git 不可用")
class MineVerifyTests(BrainTestBase):
    def _repo(self) -> Path:
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "svc.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "chore: init")
        (repo / "svc.py").write_text(
            "import timeoutlib\n\ndef run():\n    return lock_and_retry(timeout=3)\n", encoding="utf-8"
        )
        (repo / "test_svc.py").write_text("def test_run():\n    assert True\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "fix: 修复并发写入导致的重复扣款")
        return repo

    def test_mine_creates_candidates_with_resolvable_anchors(self):
        repo = self._repo()
        result = mine.mine_repo(self.conn, str(repo), limit=50)
        self.assertGreaterEqual(result["created"], 1)
        summary = verify.verify_all(self.conn)
        self.assertGreater(summary["anchors"], 0)
        self.assertEqual(summary["resolution_rate"], 1.0)

    def test_second_mine_is_idempotent(self):
        repo = self._repo()
        mine.mine_repo(self.conn, str(repo), limit=50)
        before = db.stats(self.conn)["atoms"]
        mine.mine_repo(self.conn, str(repo), limit=50)
        self.assertEqual(db.stats(self.conn)["atoms"], before)


@unittest.skipIf(shutil.which("git") is None, "git 不可用")
class IngestTests(BrainTestBase):
    def _repo(self) -> Path:
        repo = Path(self.tmp.name) / "r2"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")
        return repo

    def test_requires_anchor(self):
        result = ingest.record(self.conn, {"claim": "没有锚点的陈述不应该被写入"})
        self.assertIn("error", result)

    def test_rejects_too_short_claim(self):
        result = ingest.record(self.conn, {"claim": "并发", "path": "a.py"})
        self.assertIn("error", result)

    def test_record_with_commit_resolves_and_dedups(self):
        repo = self._repo()
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, encoding="utf-8").stdout.strip()
        payload = {
            "claim": "回调在无幂等键时重复投递会重复扣款",
            "mechanism": "上游至少一次投递，服务端无去重",
            "repo": str(repo),
            "commit": sha,
        }
        first = ingest.record(self.conn, payload)
        self.assertNotIn("error", first)
        self.assertEqual(first["anchors_resolved"], "1/1")
        again = ingest.record(self.conn, payload)
        self.assertTrue(again.get("duplicate"))

    def test_feedback_signal_tiers_and_auto_retire(self):
        aid = db.add_atom(self.conn, {"claim": "回调失败后必须退避重试", "confidence": 0.5})
        self.assertAlmostEqual(
            ingest.feedback(self.conn, aid, "used")["confidence"], 0.55, places=2)
        self.assertAlmostEqual(
            ingest.feedback(self.conn, aid, "not-used")["confidence"], 0.50, places=2)
        self.assertAlmostEqual(
            ingest.feedback(self.conn, aid, "failed")["confidence"], 0.25, places=2)
        down = ingest.feedback(self.conn, aid, "failed")
        self.assertAlmostEqual(down["confidence"], 0.0, places=2)
        self.assertEqual(down["status"], "retired")
        bad = ingest.feedback(self.conn, aid, "bogus")
        self.assertEqual(bad["reason"], "bad_outcome")

    def test_supersede_retires_old_but_keeps_history(self):
        repo = self._repo()
        old = ingest.record(self.conn, {
            "claim": "回调失败后应无限重试直到成功", "path": "a.py", "repo": str(repo)})
        new = ingest.record(self.conn, {
            "claim": "回调失败后必须退避重试并设上限，否则会拖垮下游",
            "path": "a.py",
            "repo": str(repo),
            "supersedes": old["atom_id"],
        })
        self.assertNotIn("error", new)
        self.assertEqual(new["superseded"]["superseded_by"], new["atom_id"])
        old_row = db.get_atom(self.conn, old["atom_id"])
        self.assertEqual(old_row["status"], "retired")
        self.assertEqual(old_row["superseded_by"], new["atom_id"])
        # 被取代的条目不能再出现在检索里，但 explain 仍能顺着链找到
        hits = search.search_atoms(self.conn, "回调 失败 重试", fallback=False)
        self.assertEqual([h["id"] for h in hits], [new["atom_id"]])
        self.assertEqual(list(self.conn.execute(
            "SELECT outcome FROM outcomes WHERE atom_id=?", (old["atom_id"],)))[0]["outcome"],
            "superseded")

    def test_supersede_rejects_missing_old_without_writing(self):
        before = self.conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
        result = ingest.record(self.conn, {
            "claim": "新结论不能凭空取代一个不存在的条目",
            "path": "a.py",
            "supersedes": "a-doesnotexist",
        })
        self.assertEqual(result["reason"], "missing_old")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0], before)

    def test_feedback_shares_vocabulary_and_updates_confidence(self):
        aid = db.add_atom(self.conn, {"claim": "回调在无幂等键时会重复扣款", "confidence": 0.5})
        bad = ingest.feedback(self.conn, aid, "invalidated")
        self.assertIn("error", bad)
        missing = ingest.feedback(self.conn, "a-missing", "validated")
        self.assertEqual(missing["reason"], "missing")
        ok = ingest.feedback(self.conn, aid, "validated", "本会话实测")
        self.assertAlmostEqual(ok["confidence"], 0.65, places=2)
        down = ingest.feedback(self.conn, aid, "insufficient", "没帮上")
        self.assertAlmostEqual(down["confidence"], 0.45, places=2)


class RedactTests(BrainTestBase):
    """写入前脱敏：只拦高置信密钥，不误伤正常技术文本。"""

    def test_known_patterns_are_redacted(self):
        samples = {
            "openai": "sk-abcdefghijklmnopqrstuvwxyz012345",
            "github": "ghp_abcdefghijklmnopqrstuvwxyz012345",
            "aws": "AKIAABCDEFGHIJKLMNOP",
            "google": "AIza" + "a" * 35,
            "slack": "xoxb-1234567890-abcdefghij",
            "stripe": "sk_live_abcdefghijklmnop",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5NXgL0n3I9PlFUP0THsR8U",
        }
        for kind, secret in samples.items():
            cleaned, hits = redact.clean_text(f"凭据是 {secret}")
            self.assertTrue(hits, kind)
            self.assertNotIn(secret, cleaned)

    def test_private_key_block_is_redacted(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
        cleaned, hits = redact.clean_text(text)
        self.assertIn("private_key", hits)
        self.assertNotIn("MIIEow", cleaned)

    def test_credential_keeps_key_name(self):
        cleaned, hits = redact.clean_text("api_key=abcdefghijklmnop1234")
        self.assertIn("credential", hits)
        self.assertIn("api_key=", cleaned)
        self.assertNotIn("abcdefghijklmnop1234", cleaned)

    def test_normal_technical_text_is_untouched(self):
        for text in ("contain: layout", "sk-learn 用于分类", "token 过期后重试",
                     "password 字段需要校验", "timeout=30"):
            self.assertEqual(redact.clean_text(text)[1], [], text)

    def test_ingest_redacts_claim_and_drops_secret_excerpt(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        result = ingest.record(self.conn, {
            "claim": f"配置里写死 {secret} 会把密钥带进日志",
            "path": "a.py",
            "excerpt": f"TOKEN={secret}",
        })
        self.assertNotIn("error", result)
        self.assertIn("claim", result["redacted"])
        self.assertIn("excerpt", result["redacted"])
        row = db.get_atom(self.conn, result["atom_id"])
        self.assertNotIn(secret, row["claim"])
        anchor = [a for a in db.anchors_of(self.conn, result["atom_id"]) if a["kind"] == "file"][0]
        self.assertEqual(anchor["excerpt"], "")


class DeadEndTests(BrainTestBase):
    """死路不是知识：不进 accepted 统计，只在检索与 check 时提醒别再试。"""

    def test_add_dedupes_and_redacts(self):
        secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
        first = db.add_dead_end(self.conn, "r", "a.py", f"给回调加无限重试 {secret}",
                                "下游被打爆", "隔离 fixture")
        self.assertIsInstance(first["id"], int)
        again = db.add_dead_end(self.conn, "r", "a.py", f"给回调加无限重试 {secret}",
                                "下游被打爆", "隔离 fixture")
        self.assertTrue(again.get("duplicate"))
        row = db.dead_ends_for(self.conn, path="a.py")[0]
        self.assertNotIn(secret, row["tried"])
        self.assertEqual(row["what_worked"], "隔离 fixture")

    def test_search_returns_dead_ends_even_when_no_atom_matches(self):
        db.add_dead_end(self.conn, "r", "src/pay.py", "给支付回调加无限重试",
                        "把下游打爆", "加退避并设上限")
        self.assertEqual(search.search_atoms(self.conn, "回调 重试", fallback=False), [])
        hits = search.dead_ends(self.conn, "回调 重试")
        self.assertEqual(len(hits), 1)
        self.assertIn("无限重试", hits[0]["tried"])

    def test_check_diff_lists_dead_ends_and_blocks_at_three(self):
        for i in range(3):
            db.add_dead_end(self.conn, "r", "src/pay.py", f"方案 {i}", "失败原因", "最终方案")
        diff = ("--- a/src/pay.py\n+++ b/src/pay.py\n@@ -0,0 +1,2 @@\n"
                "+def pay():\n+    return client.post(url)\n")
        result = probes.run_check(self.conn, diff)
        block = result["dead_ends"][0]
        self.assertEqual(block["path"], "src/pay.py")
        self.assertEqual(block["count"], 3)
        self.assertEqual(block["severity"], "block")
        self.assertEqual(len(block["items"]), 3)

    def test_mcp_records_dead_end_and_search_returns_it(self):
        from brain import mcp
        self.conn.execute("INSERT OR IGNORE INTO sources(path,added_at) VALUES(?,?)",
                          ("E:/proj-a", db.now()))
        self.conn.commit()
        resp = mcp.handle(self.conn, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "record_dead_end",
                                                 "arguments": {"tried": "加长超时",
                                                               "why_failed": "只是掩盖问题",
                                                               "path": "a.py"}}})
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertIsInstance(payload["id"], int)
        resp = mcp.handle(self.conn, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                      "params": {"name": "search",
                                                 "arguments": {"query": "超时"}}})
        found = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(found["results"], [])
        self.assertEqual(len(found["dead_ends"]), 1)


class BriefTests(BrainTestBase):
    """brief 是索引不是答案：硬上限，超了就截断并说明。"""

    def test_brief_is_bounded_and_says_when_truncated(self):
        for i in range(30):
            db.add_atom(self.conn, {"claim": f"第 {i} 条经验：" + "很长的结论" * 20,
                                    "status": "accepted", "confidence": 0.5})
        result = brief.build(self.conn, budget=300)
        self.assertLessEqual(result["chars"], 300)
        self.assertTrue(result["truncated"])
        self.assertIn("brief 上限", result["text"])

    def test_brief_includes_pending_recent_and_dead_ends(self):
        db.add_atom(self.conn, {"claim": "已采纳的结论", "status": "accepted", "confidence": 0.9})
        db.add_atom(self.conn, {"claim": "还没验证的候选", "status": "candidate"})
        db.add_dead_end(self.conn, "r", "a.py", "加无限重试", "把下游打爆", "退避")
        result = brief.build(self.conn)
        self.assertIn("已采纳的结论", result["text"])
        self.assertIn("还没验证的候选", result["text"])
        self.assertIn("加无限重试", result["text"])
        self.assertEqual(result["counts"]["dead_ends"], 1)

    def test_repo_filter_keeps_other_projects_out(self):
        db.add_atom(self.conn, {"claim": "甲项目经验", "status": "accepted", "repo": "E:/a"})
        db.add_atom(self.conn, {"claim": "乙项目经验", "status": "accepted", "repo": "E:/b"})
        text = brief.build(self.conn, repo="E:/a")["text"]
        self.assertIn("甲项目经验", text)
        self.assertNotIn("乙项目经验", text)


class RulesTests(BrainTestBase):
    """AGENTS.md 的托管段落只替换块内内容，块外一个字不动。"""

    def test_append_preserves_existing_content_and_is_idempotent(self):
        original = "# Repo\n\n自有规则：不要动我。\n"
        updated, state = rules.apply(original)
        self.assertEqual(state, "appended")
        self.assertTrue(updated.startswith(original.rstrip("\n")))
        self.assertIn("agentbrain-rules: 2", updated)
        again, state2 = rules.apply(updated)
        self.assertEqual(state2, "current")
        self.assertEqual(again, updated)
        self.assertEqual(again.count("## 经验库"), 1)

    def test_outdated_block_is_replaced_without_touching_surroundings(self):
        old = ("head\n<!-- agentbrain-rules: 1 -->\n## 旧规则\n<!-- /agentbrain-rules -->\ntail\n")
        updated, state = rules.apply(old)
        self.assertEqual(state, "outdated")
        self.assertIn("head", updated)
        self.assertIn("tail", updated)
        self.assertNotIn("旧规则", updated)
        self.assertEqual(rules.version_of(updated), rules.RULES_VERSION)

    def test_apply_to_reports_without_writing_by_default(self):
        repo = Path(self.tmp.name) / "docs-repo"
        repo.mkdir()
        result = rules.apply_to(str(repo))
        self.assertEqual(result["state"], "appended")
        self.assertFalse(result["written"])
        self.assertFalse((repo / "AGENTS.md").exists())


class GuardOnlyTests(BrainTestBase):
    """方案 A：guard-only 只是线索，不是知识。"""

    def _repo_with(self, body: str, subject: str) -> Path:
        repo = Path(self.tmp.name) / "g"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "svc.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")
        (repo / "svc.py").write_text(body, encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", subject)
        return repo

    def test_code_guard_creates_clue_not_atom(self):
        repo = self._repo_with("def f():\n    return call(retry=3)\n", "fix: 调整调用")
        result = mine.mine_repo(self.conn, str(repo), limit=10)
        self.assertEqual(result["created"], 0)
        self.assertGreaterEqual(result["clues_added"], 1)
        self.assertEqual(db.stats(self.conn)["atoms"], 0)

    def test_comment_guard_creates_nothing(self):
        repo = self._repo_with("def f():\n    # 幂等处理，避免重复\n    return 1\n", "fix: 补注释")
        result = mine.mine_repo(self.conn, str(repo), limit=10)
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["clues_added"], 0)

    def test_code_guard_rejects_string_literal(self):
        self.assertIsNone(mine.code_guard('raise ValueError("请稍后重试")'))
        self.assertIsNotNone(mine.code_guard("client = httpx.Client(timeout=10)"))

    def test_code_guard_rejects_docstring_and_prose(self):
        self.assertIsNone(mine.code_guard('"""第 iteration 个环节所属"遍"的 run token（--repeat 每遍唯一）。'))
        self.assertIsNone(mine.code_guard('- 跨遍/跨 run 前缀各不相同 → 幂等记录（TTL 365 天）'))
        self.assertIsNotNone(mine.code_guard("text=True, timeout=180)"))


@unittest.skipIf(shutil.which("git") is None, "git 不可用")
class EnrichTests(BrainTestBase):
    def _repo_with_fix(self) -> tuple[Path, str]:
        repo = Path(self.tmp.name) / "e"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "svc.py").write_text("def pay():\n    return charge()\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "feat: pay")
        (repo / "svc.py").write_text(
            "with lock:\n    def pay():\n        return charge()\n", encoding="utf-8")
        (repo / "test_svc.py").write_text("def test_pay():\n    assert True\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "fix: 回调并发时重复扣款")
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, encoding="utf-8").stdout.strip()
        return repo, sha

    def _atom(self, repo: Path, sha: str) -> str:
        aid = db.add_atom(self.conn, {
            "claim": "缺陷修复同时补充了测试：fix: 回调并发时重复扣款",
            "derivation": "git-signal", "repo": str(repo), "commit_sha": sha,
        })
        db.add_anchor(self.conn, aid, "file", path="svc.py", commit_sha=sha)
        return aid

    def test_accept_updates_atom_and_keeps_anchors(self):
        from brain import enrich
        repo, sha = self._repo_with_fix()
        aid = self._atom(repo, sha)

        def client(messages, model):
            return {
                "claim": "回调在无锁情况下被并发执行会导致同一订单重复扣款",
                "mechanism": "两个请求同时读到未处理状态，都通过了幂等判断并各自扣款",
                "conditions": "回调入口没有唯一索引保护且消费端多线程",
                "counter": "已用唯一索引或单线程消费时不会发生",
            }

        result = enrich.run(self.conn, only_id=aid, sleep_s=0, client=client)
        self.assertEqual(result["accepted"], 1)
        row = db.get_atom(self.conn, aid)
        self.assertEqual(row["derivation"], "llm-enriched")
        self.assertIn("唯一索引", row["counter"])
        self.assertIn("多线程", row["conditions"])
        self.assertEqual(len(db.anchors_of(self.conn, aid)), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 1)

    def test_hallucinated_path_rejected(self):
        from brain import enrich
        repo, sha = self._repo_with_fix()
        aid = self._atom(repo, sha)
        before = db.get_atom(self.conn, aid)["claim"]

        def client(messages, model):
            return {
                "claim": "src/ghost.py 中的回调未加锁会重复扣款",
                "mechanism": "并发读取未处理状态后重复执行扣款逻辑",
                "counter": "单线程消费时不成立",
            }

        result = enrich.run(self.conn, only_id=aid, sleep_s=0, client=client)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(db.get_atom(self.conn, aid)["claim"], before)

    def test_validate_rejects_generic_and_skip(self):
        from brain import enrich
        ok, why = enrich.validate({"claim": "这里需要注意并发问题，可能存在风险",
                                   "mechanism": "并发会出问题", "counter": "无"}, "old", [])
        self.assertFalse(ok)
        ok, why = enrich.validate({"skip": True, "reason": "证据不足"}, "old", [])
        self.assertFalse(ok)
        self.assertTrue(why.startswith("skip"))

    def test_validate_allows_same_claim_when_reenriching(self):
        """二次富化只为补 conditions 时，claim 没变不该被拒。"""
        from brain import enrich
        payload = {"claim": "回调在无锁情况下被并发执行会导致同一订单重复扣款",
                   "mechanism": "两个请求同时读到未处理状态并各自扣款",
                   "conditions": "回调入口没有唯一索引保护且消费端多线程",
                   "counter": "已用唯一索引或单线程消费时不会发生"}
        ok, _ = enrich.validate(payload, payload["claim"], [])
        self.assertFalse(ok)   # 首次富化：claim 与提交摘要相同 → 拒
        ok, _ = enrich.validate(payload, payload["claim"], [], require_change=False)
        self.assertTrue(ok)    # 二次富化：允许保留原 claim

    def test_select_only_picks_fix_with_test_claims(self):
        from brain import enrich
        db.add_atom(self.conn, {"claim": "新增了「timeout」防护", "derivation": "git-signal"})
        db.add_atom(self.conn, {"claim": "缺陷修复同时补充了测试：x", "derivation": "git-signal"})
        self.assertEqual([r["claim"] for r in enrich.select(self.conn)],
                         ["缺陷修复同时补充了测试：x"])

    def test_unreadable_repo_is_skipped_without_calling_model(self):
        """仓库不可读时没有任何证据，应该直接跳过而不是让模型硬编。"""
        from brain import enrich
        aid = db.add_atom(self.conn, {
            "claim": "在消费端多线程且无唯一索引时，回调重复执行会导致同一订单被扣款两次",
            "derivation": "llm-enriched", "repo": str(Path(self.tmp.name) / "ghost"),
            "conditions": "该缺陷可被测试复现"})
        called = []

        def client(messages, model):
            called.append(1)
            return {}

        result = enrich.run(self.conn, only_id=aid, sleep_s=0, client=client)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(called, [])
        self.assertEqual(db.get_atom(self.conn, aid)["conditions"], "该缺陷可被测试复现")

    def test_select_refill_picks_only_placeholder_conditions(self):
        """重跑富化的目标只有一类：已富化但 conditions 被写成占位话术。"""
        from brain import enrich
        placeholder = db.add_atom(self.conn, {
            "claim": "在消费端多线程且无唯一索引时，回调重复执行会导致同一订单被扣款两次",
            "derivation": "llm-enriched", "conditions": "该缺陷可被测试复现"})
        db.add_atom(self.conn, {
            "claim": "在消费端多线程且无唯一索引时，回调重复执行会导致同一订单被扣款两次",
            "derivation": "llm-enriched", "conditions": "消费端多线程"})
        db.add_atom(self.conn, {
            "claim": "在消费端多线程且无唯一索引时，回调重复执行会导致同一订单被扣款两次",
            "derivation": "session", "conditions": "该缺陷可被测试复现"})
        self.assertEqual([r["id"] for r in enrich.select(self.conn, refill=True)],
                         [placeholder])

    def test_commit_context_splits_subject_and_body(self):
        """二次富化时 claim 已是经验陈述，标题必须从提交里取，不能从 claim 里切。"""
        from brain import enrich
        repo, sha = self._repo_with_fix()
        subject, body, diff = enrich._commit_context(str(repo), sha, max_diff=60)
        self.assertEqual(subject, "fix: 回调并发时重复扣款")
        self.assertEqual(body, "")  # 只有标题行，正文为空
        self.assertIn("已截断", diff)
        self.assertLess(len(diff), 120)

    def test_dry_run_writes_nothing(self):
        from brain import enrich
        repo, sha = self._repo_with_fix()
        aid = self._atom(repo, sha)
        before = db.get_atom(self.conn, aid)["claim"]

        def client(messages, model):
            return {"claim": "回调被并发执行时同一订单会重复扣款",
                    "mechanism": "两个请求读到同一未处理状态并各自扣款",
                    "counter": "单线程消费或唯一索引存在时不成立"}

        result = enrich.run(self.conn, only_id=aid, sleep_s=0, client=client, dry_run=True)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(db.get_atom(self.conn, aid)["claim"], before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM revisions").fetchone()[0], 0)


class DocIngestTests(BrainTestBase):
    """文档抽取的闸门只有一道：摘录必须逐字抄得出原文。"""

    DOC = (
        "# 重试策略\n"
        "\n"
        "## 退避\n"
        "\n"
        "调用下游失败后立即重试会放大故障。实践中应先做指数退避并引入抖动，"
        "否则同一时刻的大量重试会把已经过载的服务彻底压垮，形成雪崩。"
        "退避的上限要与下游的恢复时间匹配，不能无限增长。\n"
        "\n"
        "## 目录\n"
        "\n"
        "太短。\n"
    )
    ITEM = {
        "claim": "下游失败后不做指数退避直接重试会放大故障并形成雪崩",
        "mechanism": "大量客户端在同一时刻重试，把已过载的下游彻底压垮",
        "conditions": "调用下游失败后立即重试且没有抖动与退避上限",
        "counter": "下游容量充足且重试请求量很小时不会雪崩",
        "excerpt": "否则同一时刻的大量重试会把已经过载的服务彻底压垮，形成雪崩",
    }

    def _root(self) -> Path:
        root = Path(self.tmp.name) / "docs-repo"
        (root / "docs").mkdir(parents=True)
        (root / "docs" / "retry.md").write_text(self.DOC, encoding="utf-8")
        return root

    def test_split_sections_keeps_line_range_and_drops_noise(self):
        from brain import doc_ingest
        sections = doc_ingest.split_sections(self.DOC)
        self.assertEqual([s["heading"] for s in sections], ["退避"])
        self.assertEqual(sections[0]["start"], 3)
        self.assertEqual(sections[0]["end"], 6)

    def test_find_docs_skips_vendored_dirs(self):
        from brain import doc_ingest
        root = self._root()
        (root / "node_modules").mkdir()
        (root / "node_modules" / "x.md").write_text("y" * 200, encoding="utf-8")
        names = [p.name for p in doc_ingest.find_docs(root)]
        self.assertEqual(names, ["retry.md"])

    def test_headings_inside_code_fence_are_not_sections(self):
        from brain import doc_ingest
        text = (
            "## 部署\n\n"
            "```bash\n"
            "# 这不是标题\n"
            "uv sync --locked\n"
            "```\n\n"
            + "部署前先跑 preflight 检查依赖与配置，否则服务会带着错误的配置启动。\n" * 4
        )
        sections = doc_ingest.split_sections(text)
        self.assertEqual([s["heading"] for s in sections], ["部署"])

    def test_excerpt_must_be_verbatim(self):
        from brain import doc_ingest
        section = doc_ingest.split_sections(self.DOC)[0]
        ok, _ = doc_ingest.validate_item(self.ITEM, section["text"])
        self.assertTrue(ok)
        forged = {**self.ITEM, "excerpt": "文档从未写过的句子，但看起来很像原文的一句话"}
        ok, reason = doc_ingest.validate_item(forged, section["text"])
        self.assertFalse(ok)
        self.assertIn("逐字", reason)

    def test_run_writes_candidate_with_resolved_anchor_and_dedups(self):
        from brain import doc_ingest
        root = self._root()
        result = doc_ingest.run(self.conn, str(root), sleep_s=0,
                                client=lambda messages, model: {"items": [self.ITEM]})
        self.assertEqual(result["created"], 1)
        row = self.conn.execute("SELECT * FROM atoms").fetchone()
        self.assertEqual(row["derivation"], "doc")
        anchors = db.anchors_of(self.conn, row["id"])
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["path"], "docs/retry.md")
        self.assertEqual(anchors[0]["resolved"], 1)
        self.assertEqual(row["status"], "candidate")

        again = doc_ingest.run(self.conn, str(root), sleep_s=0,
                               client=lambda messages, model: {"items": [self.ITEM]})
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["duplicates"], 1)

    def test_skip_and_dry_run_write_nothing(self):
        from brain import doc_ingest
        root = self._root()
        skipped = doc_ingest.run(self.conn, str(root), sleep_s=0,
                                 client=lambda messages, model: {"skip": True, "reason": "只是接口说明"})
        self.assertEqual(skipped["skipped"], 1)
        dry = doc_ingest.run(self.conn, str(root), sleep_s=0, dry_run=True,
                             client=lambda messages, model: {"items": [self.ITEM]})
        self.assertEqual(dry["created"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0], 0)

    def test_model_error_does_not_abort_batch(self):
        from brain import doc_ingest

        def boom(messages, model):
            raise RuntimeError("网络错误")

        result = doc_ingest.run(self.conn, str(self._root()), sleep_s=0, client=boom)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["created"], 0)

    @unittest.skipIf(shutil.which("git") is None, "git 不可用")
    def test_anchor_belongs_to_project_root_not_scan_dir(self):
        """扫描 docs/ 时经验要挂到项目根，否则和同项目的代码经验被拆成两个 repo。"""
        from brain import doc_ingest
        repo = Path(self.tmp.name) / "proj"
        (repo / "docs").mkdir(parents=True)
        _git(repo, "init", "-q")
        (repo / "docs" / "retry.md").write_text(self.DOC, encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")

        doc_ingest.run(self.conn, str(repo / "docs"), sleep_s=0,
                       client=lambda messages, model: {"items": [self.ITEM]})
        row = self.conn.execute("SELECT * FROM atoms").fetchone()
        self.assertEqual(row["repo"], str(repo))
        self.assertEqual(db.anchors_of(self.conn, row["id"])[0]["path"], "docs/retry.md")

    def test_skip_seen_spends_no_model_call_on_processed_section(self):
        from brain import doc_ingest
        root = self._root()
        doc_ingest.run(self.conn, str(root), sleep_s=0,
                       client=lambda messages, model: {"items": [self.ITEM]})
        calls = []

        def client(messages, model):
            calls.append(1)
            return {"items": [self.ITEM]}

        result = doc_ingest.run(self.conn, str(root), sleep_s=0, client=client, skip_seen=True)
        self.assertEqual(calls, [])
        self.assertEqual(result["seen_skipped"], 1)
        self.assertEqual(result["selected"], 0)


class McpTests(BrainTestBase):
    def _call(self, name: str, args: dict) -> dict:
        from brain import mcp
        resp = mcp.handle(self.conn, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})
        return json.loads(resp["result"]["content"][0]["text"])

    def test_search_without_hit_returns_empty_not_fallback(self):
        db.add_atom(self.conn, {"claim": "租约续租必须按 matched_count 判定", "confidence": 0.9})
        payload = self._call("search", {"query": "zzzqqq"})
        self.assertEqual(payload["results"], [])
        # 空结果必须说清楚是"库里没有"，并给出可执行的下一步，不能只是留白
        self.assertIn("record_atom", payload["guidance"])
        self.assertIn("record_dead_end", payload["guidance"])

    def test_tool_call_is_logged(self):
        self._call("stats", {})
        log = Path(self.tmp.name) / "logs" / "mcp-calls.jsonl"
        rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["tool"] for r in rows], ["stats"])
        self.assertTrue(rows[0]["ok"])
        self.assertIsInstance(rows[0]["ms"], int)

    def test_search_brief_records_top_score_and_tiers(self):
        from brain import mcp
        brief = mcp._result_brief("search", {"results": [
            {"id": "a-1", "score": 7.5, "tier": "L1"},
            {"id": "a-2", "score": 3.1, "tier": "L2"},
        ]}, {"limit": 4})
        self.assertEqual(brief["hits"], 2)
        self.assertEqual(brief["asked"], 4)
        self.assertEqual(brief["top_score"], 7.5)
        self.assertEqual(brief["tiers"], {"L1": 1, "L2": 1})

    def test_long_argument_is_replaced_by_length(self):
        self._call("check_diff", {"diff_text": "x" * 500})
        log = Path(self.tmp.name) / "logs" / "mcp-calls.jsonl"
        row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["args"]["diff_text"], "<500 chars>")

    def test_tool_exception_returns_error_and_keeps_session_alive(self):
        from brain import mcp
        with mock.patch.object(mcp, "_dispatch", side_effect=RuntimeError("boom")):
            resp = mcp.handle(self.conn, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": "search", "arguments": {}}})
        self.assertTrue(resp["result"]["isError"])
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("RuntimeError", payload["error"])
        # 下一次调用仍然可用：会话没有被一条坏工具拖死
        self.assertIn("atoms", self._call("stats", {}))

    def test_tool_stdout_never_leaks_into_protocol(self):
        from brain import mcp
        def noisy(conn, name, args):
            print("debug output")
            return {"ok": True}
        captured = io.StringIO()
        with mock.patch.object(mcp, "_dispatch", side_effect=noisy):
            with contextlib.redirect_stdout(captured):
                resp = mcp.handle(self.conn, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                              "params": {"name": "search", "arguments": {}}})
        self.assertEqual(captured.getvalue(), "")
        self.assertEqual(json.loads(resp["result"]["content"][0]["text"]), {"ok": True})

    def test_search_hit_count_is_logged_for_funnel(self):
        """字符数看不出有没有命中，命中率只能靠这个计数字段。"""
        db.add_atom(self.conn, {"claim": "租约续期判定"})
        self._call("search", {"query": "租约"})
        log = Path(self.tmp.name) / "logs" / "mcp-calls.jsonl"
        row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["result"]["hits"], 1)
        self.assertGreater(row["result"]["top_score"], 0)
        # token 账本：字符 / 字节 / 估算 token 都要落，后两者才能反映真实成本
        self.assertGreater(row["result_tokens"], 0)
        self.assertGreaterEqual(row["result_bytes"], row["result_chars"])

    def test_stats_carries_usage_block(self):
        payload = self._call("stats", {})
        self.assertIn("usage", payload)
        self.assertEqual(payload["usage"]["calls"], 0)


class DbTests(BrainTestBase):
    def test_stats_reports_freshness_distribution(self):
        ok = db.add_atom(self.conn, {"claim": "证据完好"})
        db.add_anchor(self.conn, ok, "file", path="a.py")
        missing = db.add_atom(self.conn, {"claim": "证据全失效"})
        db.add_anchor(self.conn, missing, "file", path="gone.py")
        db.add_atom(self.conn, {"claim": "从没校验过"})
        self.conn.execute("UPDATE anchors SET resolved=1 WHERE atom_id=?", (ok,))
        self.conn.execute("UPDATE anchors SET resolved=0 WHERE atom_id=?", (missing,))
        self.conn.commit()
        stats = db.stats(self.conn)
        self.assertEqual(stats["freshness"], {"ok": 1, "partial": 0, "unverified": 1, "missing": 1})
        self.assertIn("dead_ends", stats)

    def test_apply_enrichment_never_downgrades_confidence(self):
        aid = db.add_atom(self.conn, {"claim": "旧陈述", "confidence": 0.9})
        db.apply_enrichment(self.conn, aid, {"claim": "新陈述", "mechanism": "m", "counter": "c"})
        row = db.get_atom(self.conn, aid)
        self.assertEqual(row["derivation"], "llm-enriched")
        self.assertAlmostEqual(row["confidence"], 0.9, places=2)

    def test_apply_enrichment_raises_low_confidence_to_floor(self):
        aid = db.add_atom(self.conn, {"claim": "旧陈述", "confidence": 0.2})
        db.apply_enrichment(self.conn, aid, {"claim": "新陈述", "mechanism": "", "counter": ""})
        self.assertAlmostEqual(db.get_atom(self.conn, aid)["confidence"], 0.45, places=2)

    def test_insufficient_below_threshold_retires(self):
        aid = db.add_atom(self.conn, {"claim": "x", "confidence": 0.2})
        db.record_outcome(self.conn, aid, "insufficient")
        self.assertEqual(db.get_atom(self.conn, aid)["status"], "retired")

    def test_edge_weight_accumulates(self):
        db.add_edge(self.conn, "a-1", "same-topic", "a-2")
        db.add_edge(self.conn, "a-1", "same-topic", "a-2", weight=2)
        self.assertAlmostEqual(
            self.conn.execute("SELECT weight FROM edges").fetchone()[0], 3.0, places=2)

    def test_anchor_excerpt_is_truncated_and_deduped(self):
        aid = db.add_atom(self.conn, {"claim": "x"})
        db.add_anchor(self.conn, aid, "file", path="a.py", excerpt="z" * 900)
        db.add_anchor(self.conn, aid, "file", path="a.py", excerpt="z" * 900)
        rows = db.anchors_of(self.conn, aid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["excerpt"]), 400)

    def test_clue_dedupes_on_same_commit_path_token(self):
        for _ in range(2):
            db.add_clue(self.conn, "r", "a.py", "timeout", "x", "sha1")
        self.assertEqual(db.stats(self.conn)["clues"], 1)

    def test_stats_counts_unverified_anchors(self):
        aid = db.add_atom(self.conn, {"claim": "x"})
        db.add_anchor(self.conn, aid, "file", path="a.py")
        s = db.stats(self.conn)
        self.assertEqual(s["anchors_unverified"], 1)
        self.assertEqual(s["anchor_resolution_rate"], 0.0)

    def test_connect_creates_parent_directory(self):
        target = Path(self.tmp.name) / "deep" / "nest" / "x.db"
        conn = db.connect(target)
        self.addCleanup(conn.close)
        self.assertTrue(target.exists())

    def test_old_admitted_status_is_migrated_to_accepted(self):
        """旧库里的 admitted 必须在重连时改名，否则同一状态会有两种写法。"""
        aid = db.add_atom(self.conn, {"claim": "旧词表条目", "status": "admitted"})
        reopened = db.connect(Path(self.tmp.name) / "data" / "brain.db")
        self.addCleanup(reopened.close)
        self.assertEqual(db.get_atom(reopened, aid)["status"], "accepted")

    def test_revoke_outcome_rolls_back_confidence_and_is_idempotent(self):
        aid = db.add_atom(self.conn, {"claim": "回调在无幂等键时会重复扣款", "confidence": 0.5})
        db.record_outcome(self.conn, aid, "validated", "本会话实测")
        self.assertAlmostEqual(db.get_atom(self.conn, aid)["confidence"], 0.65, places=2)
        oid = self.conn.execute(
            "SELECT id FROM outcomes WHERE atom_id=? ORDER BY id DESC LIMIT 1", (aid,)
        ).fetchone()["id"]
        first = db.revoke_outcome(self.conn, oid)
        self.assertTrue(first["revoked"])
        self.assertAlmostEqual(first["confidence"], 0.5, places=2)
        again = db.revoke_outcome(self.conn, oid)
        self.assertAlmostEqual(again["confidence"], 0.5, places=2)
        self.assertEqual(db.revoke_outcome(self.conn, 99999)["reason"], "missing")


@unittest.skipIf(shutil.which("git") is None, "git 不可用")
class GitutilTests(BrainTestBase):
    def _repo(self) -> Path:
        repo = Path(self.tmp.name) / "g"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "feat: a")
        (repo / "a.py").write_text("x = 1\nz = 3\n", encoding="utf-8")
        (repo / "b.py").write_text("y = 2\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "fix: b")
        return repo

    def test_non_repo_is_handled_without_raising(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        self.assertFalse(gitutil.is_repo(plain))
        self.assertEqual(gitutil.commit_count(plain), 0)
        self.assertEqual(gitutil.log_commits(plain), [])

    def test_log_commits_respects_limit_and_parses_fields(self):
        repo = self._repo()
        self.assertTrue(gitutil.is_repo(repo))
        self.assertEqual(gitutil.commit_count(repo), 2)
        commits = gitutil.log_commits(repo, limit=1)
        self.assertEqual(len(commits), 1)
        self.assertEqual(commits[0]["subject"], "fix: b")
        self.assertEqual(len(commits[0]["sha"]), 40)

    def test_changed_files_added_lines_file_at_head_files(self):
        repo = self._repo()
        head = gitutil.log_commits(repo, limit=1)[0]["sha"]
        self.assertEqual(sorted(gitutil.changed_files(repo, head)), ["a.py", "b.py"])
        adds = dict(gitutil.added_lines(repo, head))
        self.assertEqual(adds["a.py"], "z = 3")
        self.assertEqual(adds["b.py"], "y = 2")
        self.assertIn("z = 3", gitutil.file_at(repo, head, "a.py"))
        self.assertIsNone(gitutil.file_at(repo, head, "ghost.py"))
        self.assertEqual(sorted(gitutil.head_files(repo)), ["a.py", "b.py"])

    def test_unknown_sha_returns_empty_results(self):
        repo = self._repo()
        missing = "0" * 40
        self.assertEqual(gitutil.changed_files(repo, missing), [])
        self.assertEqual(gitutil.added_lines(repo, missing), [])
        self.assertIsNone(gitutil.file_at(repo, missing, "a.py"))


class VerifyTests(BrainTestBase):
    def test_missing_commit_and_ghost_file_are_unresolved(self):
        aid = db.add_atom(self.conn, {"claim": "x", "repo": "/nonexistent"})
        db.add_anchor(self.conn, aid, "commit", commit_sha="deadbeef")
        db.add_anchor(self.conn, aid, "file", path="ghost.py")
        summary = verify.verify_all(self.conn)
        self.assertEqual(summary["resolution_rate"], 0.0)
        self.assertEqual(summary["anchors_resolved"], 0)

    def test_excerpt_mismatch_keeps_anchor_unresolved(self):
        repo = Path(self.tmp.name) / "w"
        repo.mkdir()
        (repo / "a.py").write_text("lock = acquire()\n", encoding="utf-8")
        aid = db.add_atom(self.conn, {"claim": "x", "repo": str(repo)})
        db.add_anchor(self.conn, aid, "file", path="a.py", excerpt="根本没出现过的片段")
        self.assertEqual(verify.verify_all(self.conn)["anchors_resolved"], 0)

    def test_path_anchor_resolves_from_worktree(self):
        repo = Path(self.tmp.name) / "w2"
        repo.mkdir()
        (repo / "a.py").write_text("lock = acquire()\n", encoding="utf-8")
        aid = db.add_atom(self.conn, {"claim": "x", "repo": str(repo)})
        db.add_anchor(self.conn, aid, "file", path="a.py", excerpt="lock = acquire()")
        summary = verify.verify_all(self.conn)
        self.assertEqual(summary["resolution_rate"], 1.0)
        self.assertEqual(summary["atoms_fully_resolved"], 1)

    def test_candidates_only_skips_accepted(self):
        aid = db.add_atom(self.conn, {"claim": "x", "status": "accepted", "repo": "/nonexistent"})
        db.add_anchor(self.conn, aid, "file", path="ghost.py")
        self.assertEqual(verify.verify_all(self.conn, only_candidates=True)["atoms_checked"], 0)
        self.assertEqual(verify.verify_all(self.conn)["atoms_checked"], 1)

    def _delete_repo(self) -> tuple[Path, str]:
        repo = Path(self.tmp.name) / "del"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "shared.png").write_text("v1\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "feat: add asset")
        (repo / "shared.png").unlink()
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "fix: remove duplicate asset")
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, encoding="utf-8").stdout.strip()
        return repo, sha

    def test_anchor_on_file_deleted_by_that_commit_resolves(self):
        """删除类经验的证据恰恰是"文件没了"，不能因为查不到就判失败。"""
        repo, sha = self._delete_repo()
        aid = db.add_atom(self.conn, {"claim": "删除共享资产会连带破坏测试 fixture", "repo": str(repo)})
        db.add_anchor(self.conn, aid, "file", path="shared.png", commit_sha=sha)
        self.assertEqual(verify.verify_atom(self.conn, db.get_atom(self.conn, aid)), (1, 1))

    def test_anchor_on_path_absent_from_commit_stays_unresolved(self):
        """放宽只针对"该提交确实动了这个文件"，写错的路径仍要判失败。"""
        repo, sha = self._delete_repo()
        aid = db.add_atom(self.conn, {"claim": "删除共享资产会连带破坏测试 fixture", "repo": str(repo)})
        db.add_anchor(self.conn, aid, "file", path="ghost.png", commit_sha=sha)
        self.assertEqual(verify.verify_atom(self.conn, db.get_atom(self.conn, aid)), (0, 1))


class SearchTests(BrainTestBase):
    def test_multi_token_query_requires_every_token(self):
        db.add_atom(self.conn, {"claim": "租约过期后旧实例仍会写入", "confidence": 0.9})
        self.assertEqual(search.search_atoms(self.conn, "租约 完全不存在", fallback=False), [])

    def test_empty_query_uses_fallback_only_when_enabled(self):
        db.add_atom(self.conn, {"claim": "唯一的高置信条目", "confidence": 0.9})
        self.assertEqual(search.search_atoms(self.conn, "", fallback=False), [])
        self.assertEqual(len(search.search_atoms(self.conn, "", fallback=True)), 1)

    def test_retired_atoms_are_excluded(self):
        db.add_atom(self.conn, {"claim": "租约过期后旧实例仍会写入", "status": "retired"})
        self.assertEqual(search.search_atoms(self.conn, "租约", fallback=False), [])

    def test_repo_filter_keeps_other_projects_out(self):
        """库里有多个项目时，同一个词会跨项目命中；按仓库收窄是唯一能区分它们的维度。"""
        db.add_atom(self.conn, {"claim": "租约过期后旧实例仍会写入", "repo": r"E:\proj-a"})
        db.add_atom(self.conn, {"claim": "租约续租必须按 matched_count 判定", "repo": r"E:\proj-b"})
        rows = search.search_atoms(self.conn, "租约", fallback=False, repo=r"E:\proj-a")
        self.assertEqual([r["repo"] for r in rows], [r"E:\proj-a"])
        # 路径写法不同（斜杠方向、末尾分隔符）也要能对上
        rows = search.search_atoms(self.conn, "租约", fallback=False, repo="E:/proj-b/")
        self.assertEqual([r["repo"] for r in rows], [r"E:\proj-b"])

    def test_repo_filter_applies_to_fallback_too(self):
        db.add_atom(self.conn, {"claim": "高置信条目", "confidence": 0.9, "repo": r"E:\proj-a"})
        rows = search.search_atoms(self.conn, "", fallback=True, repo=r"E:\proj-b")
        self.assertEqual(rows, [])

    def test_results_carry_score_and_tier(self):
        """score 用来判断命中强度，tier 用来区分"本项目的约定"与"别处的话题"。
        两者都不参与过滤，只做标注。"""
        db.add_atom(self.conn, {"claim": "租约过期后旧实例仍会写入", "confidence": 0.9,
                                "repo": r"E:\proj-a"})
        loose = search.search_atoms(self.conn, "租约", fallback=False)
        self.assertEqual(loose[0]["tier"], "L2")
        self.assertGreater(loose[0]["score"], 0)
        same_repo = search.search_atoms(self.conn, "租约", fallback=False, repo=r"E:\proj-a")
        self.assertEqual(same_repo[0]["tier"], "L1")

    def test_missing_anchor_is_excluded_from_search_and_fallback(self):
        """锚点全失效 = 证据已经查不到，不能继续占返回位。"""
        dead = db.add_atom(self.conn, {"claim": "租约过期后旧实例仍会写入", "confidence": 0.9})
        db.add_anchor(self.conn, dead, "file", path="ghost.py")
        self.conn.execute("UPDATE anchors SET resolved=0 WHERE atom_id=?", (dead,))
        self.conn.commit()
        self.assertEqual(search.search_atoms(self.conn, "租约", fallback=False), [])
        self.assertEqual(search.search_atoms(self.conn, "", fallback=True), [])

    def test_freshness_prefers_verified_and_penalizes_partial(self):
        good = db.add_atom(self.conn, {"claim": "租约续租必须校验 owner", "confidence": 0.8})
        db.add_anchor(self.conn, good, "file", path="lease.py")
        self.conn.execute("UPDATE anchors SET resolved=1 WHERE atom_id=?", (good,))
        part = db.add_atom(self.conn, {"claim": "租约续租必须校验 owner", "confidence": 0.8})
        db.add_anchor(self.conn, part, "file", path="lease.py")
        db.add_anchor(self.conn, part, "file", path="gone.py")
        self.conn.execute("UPDATE anchors SET resolved=1 WHERE atom_id=? AND path='lease.py'", (part,))
        self.conn.execute("UPDATE anchors SET resolved=0 WHERE atom_id=? AND path='gone.py'", (part,))
        self.conn.commit()
        self.assertEqual(search.freshness_of(self.conn, good), "ok")
        self.assertEqual(search.freshness_of(self.conn, part), "partial")
        rows = search.search_atoms(self.conn, "租约 续租", fallback=False)
        self.assertEqual([r["id"] for r in rows], [good, part])
        self.assertLess(rows[1]["score"], rows[0]["score"])

    def test_brief_index_omits_evidence_but_keeps_identity(self):
        """首屏只给索引：正文与锚点靠 explain 取，返回体才不会翻几倍。"""
        aid = db.add_atom(self.conn, {"claim": "租约续租必须校验 owner",
                                      "mechanism": "并发续租时两个实例都认为自己是持有者",
                                      "confidence": 0.9})
        db.add_anchor(self.conn, aid, "file", path="lease.py")
        brief = search.search_atoms(self.conn, "租约", brief=True)[0]
        self.assertEqual(brief["id"], aid)
        self.assertNotIn("anchors", brief)
        self.assertNotIn("mechanism", brief)
        self.assertIn("freshness", brief)
        full = search.search_atoms(self.conn, "租约")[0]
        self.assertIn("anchors", full)

    def test_freshness_filter_returns_only_requested_level(self):
        """过滤是精确匹配：要 partial 就只给 partial，不能把 ok 混进来。"""
        ok = db.add_atom(self.conn, {"claim": "租约续租必须校验 owner", "confidence": 0.8})
        db.add_anchor(self.conn, ok, "file", path="lease.py")
        part = db.add_atom(self.conn, {"claim": "租约续租必须校验 owner", "confidence": 0.8})
        db.add_anchor(self.conn, part, "file", path="lease.py")
        db.add_anchor(self.conn, part, "file", path="gone.py")
        self.conn.execute("UPDATE anchors SET resolved=1 WHERE atom_id=?", (ok,))
        self.conn.execute("UPDATE anchors SET resolved=1 WHERE atom_id=? AND path='lease.py'", (part,))
        self.conn.execute("UPDATE anchors SET resolved=0 WHERE atom_id=? AND path='gone.py'", (part,))
        self.conn.commit()
        rows = search.search_atoms(self.conn, "租约 续租", fallback=False, freshness="partial")
        self.assertEqual([r["id"] for r in rows], [part])
        self.assertEqual(search.search_atoms(self.conn, "租约 续租", fallback=False,
                                             freshness="ok")[0]["id"], ok)

    def test_same_stack_breaks_tie(self):
        """同栈只是排序偏好：两条同样命中时，同栈的排前面。"""
        py = Path(self.tmp.name) / "proj-py"
        java = Path(self.tmp.name) / "proj-java"
        py.mkdir()
        java.mkdir()
        (py / "pyproject.toml").write_text("", encoding="utf-8")
        (java / "pom.xml").write_text("", encoding="utf-8")
        db.add_atom(self.conn, {"claim": "租约续租必须校验 owner", "confidence": 0.8,
                                "repo": str(py)})
        db.add_atom(self.conn, {"claim": "租约续租必须校验 owner", "confidence": 0.8,
                                "repo": str(java)})
        first = search.search_atoms(self.conn, "租约 续租", fallback=False, stack="python")
        self.assertEqual(Path(first[0]["repo"]).name, "proj-py")
        second = search.search_atoms(self.conn, "租约 续租", fallback=False, stack="java")
        self.assertEqual(Path(second[0]["repo"]).name, "proj-java")

    def test_stack_is_auto_detected_from_cwd(self):
        """不传 stack 时按当前目录识别，调用方不必手填。"""
        py = Path(self.tmp.name) / "auto-py"
        java = Path(self.tmp.name) / "auto-java"
        py.mkdir()
        java.mkdir()
        (py / "pyproject.toml").write_text("", encoding="utf-8")
        (java / "pom.xml").write_text("", encoding="utf-8")
        db.add_atom(self.conn, {"claim": "租约续租必须校验 owner", "confidence": 0.8,
                                "repo": str(py)})
        db.add_atom(self.conn, {"claim": "租约续租必须校验 owner", "confidence": 0.8,
                                "repo": str(java)})
        with mock.patch.object(search.os, "getcwd", return_value=str(py)):
            rows = search.search_atoms(self.conn, "租约 续租", fallback=False)
        self.assertEqual(Path(rows[0]["repo"]).name, "auto-py")
        self.assertEqual(search.current_stack(str(java)), "java")

    def test_related_marks_same_file_as_l0(self):
        aid = db.add_atom(self.conn, {"claim": "租约续期用 modified_count 判定会误报"})
        db.add_anchor(self.conn, aid, "file", path="src/repo/lease.py")
        hit = search.related(self.conn, "src/repo/lease.py", limit=1)[0]
        self.assertEqual(hit["tier"], "L0")
        self.assertGreater(hit["score"], 0)

    def test_linked_edges_are_expanded_only_on_request(self):
        """默认不带一跳邻居：返回体翻倍，而调用方多数只看前三条的 claim。"""
        a = db.add_atom(self.conn, {"claim": "甲方经验"})
        b = db.add_atom(self.conn, {"claim": "乙方经验"})
        db.add_edge(self.conn, a, "similar", b)
        plain = search.search_atoms(self.conn, "甲方", fallback=False)
        self.assertNotIn("linked", plain[0])
        hits = search.search_atoms(self.conn, "甲方", fallback=False, expand=True)
        self.assertEqual(hits[0]["linked"][0]["id"], b)
        self.assertEqual(hits[0]["linked"][0]["claim"], "乙方经验")

    def test_relative_floor_drops_weak_matches(self):
        """同一查询里只保留接近最佳命中的条目，不再"凑满限额"。"""
        strong = db.add_atom(self.conn, {
            "claim": "租约续租必须同时校验 owner 与幂等键",
            "mechanism": "并发续租时两个实例都认为自己是持有者",
            "confidence": 0.9})
        weak = db.add_atom(self.conn, {
            "claim": "租约 幂等",
            "confidence": 0.3})
        rows = search.search_atoms(self.conn, "租约 续租 幂等", fallback=False, limit=5)
        ids = [r["id"] for r in rows]
        self.assertIn(strong, ids)
        self.assertNotIn(weak, ids)

    def test_coverage_slack_drops_topically_distant_match(self):
        """最佳命中覆盖全部检索词时，只中一半的条目属于跨话题噪声（分数可能还不低）。"""
        strong = db.add_atom(self.conn, {
            "claim": "租约 幂等 续租 owner 必须同时校验",
            "confidence": 0.9})
        far = db.add_atom(self.conn, {
            "claim": "租约 幂等",
            "mechanism": "租约 幂等",
            "confidence": 0.9})
        rows = search.search_atoms(self.conn, "租约 幂等 续租 owner", fallback=False, limit=5)
        ids = [r["id"] for r in rows]
        self.assertIn(strong, ids)
        self.assertNotIn(far, ids)

    def test_same_file_match_requires_exact_basename(self):
        """`%a.py` 会把 my_a.py 也算成同一个文件，必须按分隔符对齐。"""
        same = db.add_atom(self.conn, {"claim": "租约续期判定"})
        db.add_anchor(self.conn, same, "file", path="src/my_a.py")
        self.assertEqual(search.related(self.conn, "src/a.py"), [])
        self.assertEqual([h["id"] for h in search.related(self.conn, "src/my_a.py")], [same])

    def test_underscore_in_filename_is_not_a_like_wildcard(self):
        """SQL LIKE 的 `_` 是通配符，而代码文件名几乎都带下划线。"""
        other = db.add_atom(self.conn, {"claim": "租约续期判定"})
        db.add_anchor(self.conn, other, "file", path="src/leaseXmanager.py")
        self.assertEqual(search.related(self.conn, "src/lease_manager.py"), [])
        real = db.add_atom(self.conn, {"claim": "租约续期判定"})
        db.add_anchor(self.conn, real, "file", path="src/lease_manager.py")
        self.assertEqual([h["id"] for h in search.related(self.conn, "src/lease_manager.py")], [real])

    def test_rare_stem_only_counts_when_hint_present(self):
        lease = db.add_atom(self.conn, {"claim": "租约续期判定", "confidence": 0.4})
        db.add_anchor(self.conn, lease, "file", path="src/repo/lease_manager.py")
        self.assertEqual(search.related(self.conn, "src/conversation/request_lease.py"), [])
        hits = search.related(self.conn, "src/conversation/request_lease.py", hint="租约")
        self.assertEqual([h["id"] for h in hits], [lease])

    def test_more_token_hits_in_claim_rank_first(self):
        """同置信度下不能再靠插入顺序排：命中集中在前置字段的要靠前。"""
        spread = db.add_atom(self.conn, {
            "claim": "租约过期后的处理", "counter": "幂等靠唯一键兜住", "confidence": 0.5})
        focused = db.add_atom(self.conn, {
            "claim": "租约过期要靠幂等键兜住重复写入", "confidence": 0.5})
        hits = search.search_atoms(self.conn, "租约 幂等", fallback=False)
        self.assertEqual([h["id"] for h in hits], [focused, spread])

    def test_accepted_atom_outranks_candidate_with_same_text(self):
        candidate = db.add_atom(self.conn, {"claim": "回调缺幂等键会重复扣款", "confidence": 0.5})
        accepted = db.add_atom(self.conn, {"claim": "回调缺幂等键会重复扣款", "confidence": 0.5,
                                           "status": "accepted"})
        hits = search.search_atoms(self.conn, "幂等", fallback=False)
        self.assertEqual([h["id"] for h in hits], [accepted, candidate])

    def test_tokenize_keeps_latin_words_and_uses_chinese_bigrams(self):
        self.assertEqual(search.tokenize("lease_owner 回看候选"),
                         ["lease_owner", "回看", "看候", "候选"])
        self.assertEqual(search.tokenize("回看"), ["回看"])   # 两字不切
        self.assertEqual(search.tokenize("在"), [])           # 单字无信息量

    def test_min_hits_relaxes_only_long_queries(self):
        self.assertEqual([search.min_hits(n) for n in (1, 2, 3, 4, 5, 7)], [1, 2, 2, 2, 2, 3])

    def test_long_chinese_query_no_longer_returns_empty(self):
        """「回看候选间隔排序」这类连写查询，旧实现会整段当一个词，必然零命中。"""
        db.add_atom(self.conn, {"claim": "回看候选要按时间窗收口，否则同分类会被永久挡住"})
        self.assertTrue(search.search_atoms(self.conn, "回看候选间隔排序", fallback=False))

    def test_long_query_only_needs_half_coverage(self):
        db.add_atom(self.conn, {"claim": "幂等写库要靠唯一键兜住重复扣款"})
        self.assertTrue(search.search_atoms(self.conn, "幂等 唯一键 租约 续租", fallback=False))
        # 三词只中一个：低于一半，仍要判空，避免退化成"随便给点结果"
        self.assertEqual(search.search_atoms(self.conn, "幂等 租约 续租", fallback=False), [])


class TriageTests(BrainTestBase):
    def _atom(self, *, resolved: int = 1, **fields) -> str:
        payload = {
            "claim": "在消费端多线程且无唯一索引时，回调重复执行会导致同一订单被扣款两次",
            "mechanism": "两个请求同时读到未处理状态，各自执行扣款后都写回成功",
            "conditions": "回调入口没有唯一索引保护，且消费端是多线程",
            "counter": "已用唯一索引或单线程消费时不会发生",
        }
        payload.update(fields)
        aid = db.add_atom(self.conn, payload)
        db.add_anchor(self.conn, aid, "file", path="src/a.py", excerpt="x")
        if resolved:
            self.conn.execute("UPDATE anchors SET resolved=1 WHERE atom_id=?", (aid,))
            self.conn.commit()
        return aid

    def test_classify_four_buckets(self):
        cases = {
            "retire": {"mechanism": ""},
            "hold": {"counter": ""},
            "refill": {"conditions": "该缺陷可被测试复现"},
            "accept": {},
        }
        for expected, fields in cases.items():
            with self.subTest(bucket=expected):
                aid = self._atom(**fields)
                row = db.get_atom(self.conn, aid)
                self.assertEqual(refine.classify(self.conn, row)[0], expected)

    def test_unresolved_anchor_is_retired(self):
        aid = self._atom(resolved=0)
        self.assertEqual(refine.classify(self.conn, db.get_atom(self.conn, aid))[0], "retire")

    def test_dry_run_changes_nothing(self):
        aid = self._atom()
        result = refine.triage(self.conn)
        self.assertEqual(result["counts"]["accept"], 1)
        self.assertFalse(result["applied"])
        self.assertEqual(db.get_atom(self.conn, aid)["status"], "candidate")

    def test_apply_retires_and_accepts_but_leaves_refill(self):
        good = self._atom()
        bad = self._atom(mechanism="")
        refill = self._atom(conditions="该缺陷可被测试复现")
        result = refine.triage(self.conn, apply=True)
        self.assertEqual(result["counts"]["accept"], 1)
        self.assertEqual(db.get_atom(self.conn, good)["status"], "accepted")
        self.assertEqual(db.get_atom(self.conn, bad)["status"], "retired")
        self.assertEqual(db.get_atom(self.conn, refill)["status"], "candidate")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0], 2)

    def test_apply_is_idempotent(self):
        self._atom()
        refine.triage(self.conn, apply=True)
        again = refine.triage(self.conn, apply=True)
        self.assertEqual(sum(again["counts"].values()), 0)


class RevisionTests(BrainTestBase):
    def test_save_revision_records_all_four_fields(self):
        aid = db.add_atom(self.conn, {"claim": "新陈述"})
        db.save_revision(self.conn, aid,
                         {"claim": "旧陈述", "mechanism": "旧机制",
                          "conditions": "旧条件", "counter": "旧反例"},
                         {"claim": "新陈述", "mechanism": "新机制",
                          "conditions": "新条件", "counter": "新反例"},
                         "model", "accepted")
        row = self.conn.execute("SELECT * FROM revisions").fetchone()
        self.assertEqual(row["old_mechanism"], "旧机制")
        self.assertEqual(row["old_conditions"], "旧条件")
        self.assertEqual(row["new_conditions"], "新条件")

    def test_revert_restores_four_fields_and_leaves_a_trace(self):
        aid = db.add_atom(self.conn, {"claim": "新陈述", "mechanism": "新机制",
                                      "conditions": "新条件", "counter": "新反例"})
        db.save_revision(self.conn, aid,
                         {"claim": "旧陈述", "mechanism": "旧机制",
                          "conditions": "旧条件", "counter": "旧反例"},
                         {"claim": "新陈述", "mechanism": "新机制",
                          "conditions": "新条件", "counter": "新反例"},
                         "model", "accepted")
        rid = self.conn.execute("SELECT id FROM revisions").fetchone()[0]
        self.assertEqual(refine.revert_revision(self.conn, rid)["reverted_from"], rid)
        row = db.get_atom(self.conn, aid)
        self.assertEqual((row["claim"], row["mechanism"], row["conditions"], row["counter"]),
                         ("旧陈述", "旧机制", "旧条件", "旧反例"))
        verdicts = [r["verdict"] for r in self.conn.execute(
            "SELECT verdict FROM revisions ORDER BY id")]
        self.assertEqual(verdicts, ["accepted", "reverted"])

    def test_revert_missing_revision_reports_error(self):
        self.assertIn("error", refine.revert_revision(self.conn, 999))


class AuditTests(BrainTestBase):
    DIFF = (
        "--- a/src/pay.py\n+++ b/src/pay.py\n@@ -1,1 +3,2 @@\n"
        "+def pay(order):\n"
        "+    return client.post(url, json=order)\n"
    )

    def _atom(self, *, claim="在网关超时缺失时下单会永久挂起", path="src/pay.py") -> str:
        aid = db.add_atom(self.conn, {
            "claim": claim,
            "mechanism": "请求没有超时预算，服务端不响应时连接永久占用",
            "conditions": "调用外部网关的下单路径",
            "counter": "调用方自己设了外层超时则不成立",
        })
        db.add_anchor(self.conn, aid, "file", path=path)
        return aid

    def test_violation_with_real_evidence_is_kept(self):
        from brain import audit
        aid = self._atom()

        def client(messages, model):
            return {"results": [{"atom_id": aid, "verdict": "violates",
                                 "evidence": "return client.post(url, json=order)",
                                 "reason": "新增的下单调用没有超时"}]}

        result = audit.run(self.conn, self.DIFF, client=client)
        self.assertEqual(result["candidates"], 1)
        self.assertEqual(result["results"][0]["verdict"], "violates")
        self.assertIn("client.post", result["results"][0]["evidence"])
        self.assertEqual(result["rejected_evidence"], 0)

    def test_fabricated_evidence_is_voided(self):
        """抄不出 diff 里的原行就作废——这是这套机制唯一可信的地方。"""
        from brain import audit
        aid = self._atom()

        def client(messages, model):
            return {"results": [{"atom_id": aid, "verdict": "violates",
                                 "evidence": "timeout=30  # 实际并不存在",
                                 "reason": "编的"}]}

        result = audit.run(self.conn, self.DIFF, client=client)
        self.assertEqual(result["rejected_evidence"], 1)
        self.assertEqual(result["results"][0]["verdict"], "rejected")
        self.assertNotIn("evidence", result["results"][0])

    def test_violation_is_recorded_as_lead_without_touching_confidence(self):
        """LLM 报出的命中只是线索：要留下痕迹，但不能给自己加分。"""
        from brain import audit
        aid = self._atom()
        before = db.get_atom(self.conn, aid)

        def client(messages, model):
            return {"results": [{"atom_id": aid, "verdict": "violates",
                                 "evidence": "return client.post(url, json=order)",
                                 "reason": "新增的下单调用没有超时"}]}

        result = audit.run(self.conn, self.DIFF, client=client)
        self.assertEqual(result["recorded"], 1)
        after = db.get_atom(self.conn, aid)
        self.assertAlmostEqual(after["confidence"], before["confidence"], places=2)
        self.assertEqual(after["status"], "candidate")
        note = self.conn.execute("SELECT note FROM outcomes").fetchone()[0]
        self.assertTrue(note.startswith("audit:"))
        self.assertIn("client.post", note)

    def test_record_flag_can_be_disabled(self):
        from brain import audit
        aid = self._atom()

        def client(messages, model):
            return {"results": [{"atom_id": aid, "verdict": "violates",
                                 "evidence": "return client.post(url, json=order)"}]}

        result = audit.run(self.conn, self.DIFF, client=client, record=False)
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0], 0)

    def test_only_verified_violations_are_recorded(self):
        """not_applicable 与证据作废的 violates 都不该写入反馈。"""
        from brain import audit
        aid = self._atom()

        def client(messages, model):
            return {"results": [
                {"atom_id": aid, "verdict": "not_applicable", "reason": "不涉及"},
                {"atom_id": aid, "verdict": "violates", "evidence": "timeout=30 并不存在"},
            ]}

        result = audit.run(self.conn, self.DIFF, client=client)
        self.assertEqual(result["recorded"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0], 0)

    def test_unknown_atom_and_bad_verdict_are_dropped(self):
        from brain import audit

        def client(messages, model):
            return {"results": [{"atom_id": "a-ghost", "verdict": "violates"},
                                {"atom_id": "a-ghost", "verdict": "maybe"}]}

        result = audit.run(self.conn, self.DIFF, atom_ids=["a-ghost"], client=client)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["results"], [])

    def test_no_candidate_is_reported_not_silently_empty(self):
        from brain import audit

        def client(messages, model):
            raise AssertionError("没有候选时不该调用模型")

        result = audit.run(self.conn, self.DIFF, client=client)
        self.assertEqual(result["candidates"], 0)
        self.assertIn("没有已沉淀经验", result["note"])

    def test_explicit_atom_ids_override_path_matching(self):
        from brain import audit
        far = self._atom(claim="另一个文件上的经验", path="src/other.py")
        seen = {}

        def client(messages, model):
            seen["text"] = messages[-1]["content"]
            return {"results": []}

        audit.run(self.conn, self.DIFF, atom_ids=[far], client=client)
        self.assertIn("另一个文件上的经验", seen["text"])

    def test_retired_atom_is_not_a_candidate(self):
        from brain import audit
        aid = self._atom()
        self.conn.execute("UPDATE atoms SET status='retired' WHERE id=?", (aid,))
        self.conn.commit()
        self.assertEqual(audit.run(self.conn, self.DIFF, atom_ids=[aid],
                                   client=lambda m, model: {"results": []})["candidates"], 0)


class FunnelTests(BrainTestBase):
    def _log(self, *rows) -> None:
        path = Path(self.tmp.name) / "logs"
        path.mkdir(exist_ok=True)
        (path / "mcp-calls.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8")

    def test_usage_ignores_calls_without_structured_result(self):
        """旧日志只有字符数，看不出有没有命中，不能混进命中率。"""
        self._log(
            {"ts": "2026-10-30T10:00:00+08:00", "tool": "search", "ok": True, "ms": 3,
             "result": {"hits": 2}},
            {"ts": "2026-10-30T10:01:00+08:00", "tool": "search", "ok": True, "ms": 1,
             "result": {"hits": 0}},
            {"ts": "2026-10-30T10:02:00+08:00", "tool": "check_diff", "ok": True, "ms": 5,
             "result": {"findings": 1}},
            {"ts": "2026-10-30T10:03:00+08:00", "tool": "search", "ok": True, "ms": 1},
        )
        usage = funnel.usage(days=7, today=date(2026, 10, 30))
        self.assertEqual(usage["calls"], 4)
        self.assertEqual((usage["search_calls"], usage["search_hit_rate"]), (2, 0.5))
        self.assertEqual((usage["check_calls"], usage["check_hit_rate"]), (1, 1.0))
        self.assertEqual(usage["unjudged"], 1)

    def test_usage_window_excludes_old_calls(self):
        self._log(
            {"ts": "2026-09-01T10:00:00+08:00", "tool": "search", "ok": True, "ms": 1,
             "result": {"hits": 1}},
            {"ts": "2026-10-30T10:00:00+08:00", "tool": "search", "ok": True, "ms": 1,
             "result": {"hits": 1}},
        )
        self.assertEqual(funnel.usage(days=7, today=date(2026, 10, 30))["calls"], 1)

    def test_usage_without_log_file_is_zero(self):
        usage = funnel.usage(days=7, today=date(2026, 10, 30))
        self.assertEqual(usage["calls"], 0)
        self.assertIsNone(usage["search_hit_rate"])

    def test_feedback_separates_machine_grading_from_real_use(self):
        auto = db.add_atom(self.conn, {"claim": "机器采纳的条目", "status": "accepted"})
        db.record_outcome(self.conn, auto, "validated", "triage: 机检通过（未逐条人工复核）")
        real = db.add_atom(self.conn, {"claim": "真实用过的条目", "status": "accepted"})
        db.record_outcome(self.conn, real, "validated", "本会话实测：两个脚本均带 BOM")
        feedback = funnel.feedback(self.conn)
        self.assertEqual(feedback["real_feedback_atoms"], 1)
        self.assertEqual(feedback["auto_graded"], 1)
        # accepted 2 条，只有 1 条被真实使用验证过
        self.assertEqual(feedback["accepted_without_real_feedback"], 1)

    def test_audit_hits_are_leads_not_real_feedback(self):
        """audit 记的疑似命中不能把"真实使用验证"的数量虚高。"""
        aid = db.add_atom(self.conn, {"claim": "条目", "status": "accepted"})
        db.record_outcome(self.conn, aid, "validated", "audit: return client.post(...)",
                          adjust=False)
        feedback = funnel.feedback(self.conn)
        self.assertEqual(feedback["audit_hits"], 1)
        self.assertEqual(feedback["audit_atoms"], 1)
        self.assertEqual(feedback["real_feedback_atoms"], 0)
        self.assertEqual(feedback["accepted_without_real_feedback"], 1)

    def test_report_covers_four_layers(self):
        db.add_atom(self.conn, {"claim": "条目", "status": "accepted"})
        report = funnel.report(self.conn, days=7)
        self.assertEqual(set(report), {"knowledge", "processing", "usage", "feedback",
                                       "probes", "probe_feedback"})
        self.assertEqual(report["knowledge"]["by_status"], {"accepted": 1})

    def test_usage_reports_token_and_return_ledger(self):
        """token 账本要能回答「每次检索平均花多少、返回几条」。"""
        self._log(
            {"ts": "2026-10-30T10:00:00+08:00", "tool": "search", "ok": True, "ms": 2,
             "result_tokens": 80, "result_bytes": 300, "result": {"hits": 1}},
            {"ts": "2026-10-30T10:01:00+08:00", "tool": "search", "ok": True, "ms": 2,
             "result_tokens": 120, "result_bytes": 500, "result": {"hits": 3}},
            {"ts": "2026-10-30T10:02:00+08:00", "tool": "search", "ok": True, "ms": 2,
             "result_tokens": 200, "result_bytes": 700, "result": {"hits": 2}},
            # 旧日志没有 token 字段，不能当成 0 拉低均值
            {"ts": "2026-10-30T10:03:00+08:00", "tool": "search", "ok": True, "ms": 2,
             "result": {"hits": 9}},
        )
        usage = funnel.usage(days=7, today=date(2026, 10, 30))
        self.assertEqual(usage["search_tokens_p50"], 120)
        self.assertEqual(usage["search_tokens_p90"], 200)
        self.assertEqual(usage["search_bytes_p50"], 500)
        self.assertEqual(usage["search_returned_avg"], 2.0)

    def test_probe_false_positive_downgrades_to_clue(self):
        """误报率过高的探针不再告警，但仍以 clue 留痕。"""
        aid = db.add_atom(self.conn, {"claim": "超时经验", "confidence": 0.8})
        for _ in range(3):
            db.record_outcome(self.conn, aid, "probe_false_positive",
                              "probe:demo-noisy")
        path = Path(self.tmp.name) / "logs"
        path.mkdir(exist_ok=True)
        (path / "mcp-calls.jsonl").write_text("".join(
            json.dumps({"ts": "2026-10-30T10:00:00+08:00", "tool": "check_diff",
                        "ok": True, "ms": 1, "result": {"findings": 1,
                                                        "probes": ["demo-noisy"]}}) + "\n"
            for _ in range(3)), encoding="utf-8")
        ratios = probes.probe_ratios(self.conn)
        self.assertTrue(ratios["demo-noisy"]["downgrade"])

        probe = {"id": "demo-noisy", "added_any": [r"requests\.get\("],
                 "severity": "high", "message": "网络调用缺超时"}
        diff = ("--- a/src/net.py\n+++ b/src/net.py\n@@ -0,0 +1,2 @@\n"
                "+resp = requests.get(url)\n+return resp\n")
        with mock.patch.object(probes, "load_probes", return_value=[probe]):
            result = probes.run_check(self.conn, diff)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["downgraded_hits"], 1)
        self.assertIn("demo-noisy", result["downgraded"])
        clues = list(self.conn.execute("SELECT token, path FROM clues"))
        self.assertEqual([(c["token"], c["path"]) for c in clues],
                         [("demo-noisy", "src/net.py")])
        self.assertEqual(db.get_atom(self.conn, aid)["confidence"], 0.5)

    def test_usage_reports_top_score_full_rate_and_tiers(self):
        """判断"检索变差"还是"库变杂"要看 top_score 与满额率的走势，单看命中率不够。"""
        self._log(
            {"ts": "2026-10-30T10:00:00+08:00", "tool": "search", "ok": True, "ms": 2,
             "result": {"hits": 3, "asked": 3, "top_score": 4.1, "tiers": {"L2": 3}}},
            {"ts": "2026-10-30T10:01:00+08:00", "tool": "search", "ok": True, "ms": 2,
             "result": {"hits": 3, "asked": 3, "top_score": 6.0,
                        "tiers": {"L1": 2, "L2": 1}}},
            {"ts": "2026-10-30T10:02:00+08:00", "tool": "search", "ok": True, "ms": 2,
             "result": {"hits": 1, "asked": 3, "top_score": 9.4, "tiers": {"L0": 1}}},
        )
        usage = funnel.usage(days=7, today=date(2026, 10, 30))
        self.assertEqual(usage["search_graded"], 3)
        self.assertEqual(usage["search_top_score_p50"], 6.0)
        self.assertEqual(usage["search_top_score_p90"], 9.4)
        self.assertEqual(usage["search_full_rate"], 0.667)
        self.assertEqual(usage["search_tiers"], {"L2": 4, "L1": 2, "L0": 1})


class EvaluateTests(BrainTestBase):
    """评测集是用来防"改完感觉变好了"的，口径必须能被测试钉住。"""

    def _write(self, cases: list[dict]) -> str:
        path = Path(self.tmp.name) / "queries.json"
        path.write_text(json.dumps({"queries": cases}, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_recall_and_empty_expectation(self):
        from brain import evaluate
        aid = db.add_atom(self.conn, {"claim": "租约过期后旧实例仍会写入", "confidence": 0.9})
        path = self._write([
            {"query": "租约", "expect": [aid]},
            {"query": "zzzqqq", "expect": []},
        ])
        result = evaluate.run(self.conn, path=path)
        self.assertEqual(result["recall_at_1"], 1.0)
        self.assertEqual(result["recall_at_3"], 1.0)
        self.assertEqual(result["zero_hit"], 0)
        self.assertEqual(result["false_positives"], 0)

    def test_expected_but_missing_counts_as_zero_hit(self):
        from brain import evaluate
        path = self._write([{"query": "zzzqqq", "expect": ["a-missing"]}])
        result = evaluate.run(self.conn, path=path)
        self.assertEqual(result["recall_at_3"], 0.0)
        self.assertEqual(result["zero_hit"], 1)

    def test_hits_on_empty_expectation_are_false_positives(self):
        from brain import evaluate
        aid = db.add_atom(self.conn, {"claim": "租约过期后旧实例仍会写入"})
        path = self._write([{"query": "租约", "expect": []}])
        result = evaluate.run(self.conn, path=path)
        self.assertEqual(result["false_positives"], 1)
        self.assertEqual(result["abstention_accuracy"], 0.0)
        self.assertEqual(result["rows"][0]["got"], [aid])

    def test_scope_isolation_and_stale_rejection(self):
        from brain import evaluate
        good = db.add_atom(self.conn, {"claim": "租约续租必须校验 owner",
                                       "repo": r"E:\proj-a"})
        db.add_atom(self.conn, {"claim": "租约续租必须校验 owner",
                                "repo": r"E:\proj-b"})
        dead = db.add_atom(self.conn, {"claim": "租约 幂等 旧结论", "repo": r"E:\proj-a"})
        db.add_anchor(self.conn, dead, "file", path="ghost.py")
        self.conn.execute("UPDATE anchors SET resolved=0 WHERE atom_id=?", (dead,))
        self.conn.commit()
        path = self._write([
            {"query": "租约 续租", "expect": [good], "repo": r"E:\proj-a"},
            {"query": "租约 幂等", "expect": [dead]},
        ])
        result = evaluate.run(self.conn, path=path)
        self.assertEqual(result["scope_isolation"], 1.0)
        self.assertEqual(result["rows"][0]["leaked"], 0)
        self.assertEqual(result["stale_shown"], 0)
        self.assertEqual(result["stale_rejection"], 1.0)
        self.assertEqual(result["rows"][1]["got"], [])

    def test_budget_and_latency_are_reported(self):
        from brain import evaluate
        aid = db.add_atom(self.conn, {"claim": "租约过期后旧实例仍会写入", "confidence": 0.9})
        path = self._write([{"query": "租约", "expect": [aid]}])
        result = evaluate.run(self.conn, path=path)
        self.assertEqual(result["over_budget"], 0)
        self.assertGreater(result["token_budget_bytes"], 0)
        self.assertGreaterEqual(result["latency_p95_ms"], result["latency_p50_ms"])


class CallLogTests(BrainTestBase):
    """调用日志会无限增长，轮转必须保留一份历史且读写口径一致。"""

    def test_rotation_keeps_one_history_file(self):
        from brain import calllog
        with mock.patch.object(calllog, "MAX_BYTES", 1):
            calllog.append({"tool": "search", "ts": "2026-10-01T00:00:00"})
            calllog.append({"tool": "stats", "ts": "2026-10-02T00:00:00"})
            calllog.append({"tool": "explain", "ts": "2026-10-03T00:00:00"})
        self.assertTrue(calllog.rotated().is_file())
        self.assertEqual([r["tool"] for r in calllog.read()], ["stats", "explain"])

    def test_funnel_counts_calls_from_rotated_file(self):
        from brain import calllog
        with mock.patch.object(calllog, "MAX_BYTES", 1):
            calllog.append({"tool": "search", "ok": True, "ms": 2,
                            "ts": "2026-10-29T10:00:00+08:00", "result": {"hits": 1}})
            calllog.append({"tool": "check_diff", "ok": True, "ms": 4,
                            "ts": "2026-10-30T10:00:00+08:00", "result": {"findings": 0}})
        usage = funnel.usage(days=7, today=date(2026, 10, 30))
        self.assertEqual(usage["calls"], 2)
        self.assertEqual(usage["search_hit_rate"], 1.0)

    def test_read_skips_corrupt_lines(self):
        from brain import calllog
        path = calllog.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"tool": "search"}\nnot-json\n', encoding="utf-8")
        self.assertEqual(len(calllog.read()), 1)


class LinkTests(BrainTestBase):
    def _atom(self, repo: str, *, claim: str, commit: str = "", path: str = "") -> str:
        aid = db.add_atom(self.conn, {"claim": claim, "repo": repo, "commit_sha": commit})
        if commit:
            db.add_anchor(self.conn, aid, "commit", commit_sha=commit, excerpt=claim)
        if path:
            db.add_anchor(self.conn, aid, "file", path=path, commit_sha=commit)
        return aid

    def _edges(self) -> dict:
        return {(r["src"], r["rel"], r["dst"]): r["weight"]
                for r in self.conn.execute("SELECT * FROM edges")}

    def test_shared_file_links_both_directions(self):
        first = self._atom("/r", claim="甲经验", path="src/a.py")
        second = self._atom("/r", claim="乙经验", path="src/a.py")
        result = link.rebuild(self.conn)
        edges = self._edges()
        self.assertEqual(result["file_pairs"], 1)
        self.assertIn((first, "same-file", second), edges)
        self.assertIn((second, "same-file", first), edges)

    def test_shared_commit_links(self):
        first = self._atom("/r", claim="甲经验", commit="sha-1", path="src/a.py")
        second = self._atom("/r", claim="乙经验", commit="sha-1", path="src/b.py")
        link.rebuild(self.conn)
        self.assertIn((first, "same-commit", second), self._edges())

    def test_same_file_name_in_different_repos_is_not_linked(self):
        self._atom("/r1", claim="甲经验", path="src/a.py")
        self._atom("/r2", claim="乙经验", path="src/a.py")
        self.assertEqual(link.rebuild(self.conn)["edges"], 0)

    def test_rebuild_is_idempotent_and_does_not_pile_up_weight(self):
        self._atom("/r", claim="甲经验", path="src/a.py")
        self._atom("/r", claim="乙经验", path="src/a.py")
        link.rebuild(self.conn)
        before = self._edges()
        link.rebuild(self.conn)
        self.assertEqual(self._edges(), before)

    def test_retired_atom_is_not_linked(self):
        """退役的知识不该继续以"相关"的身份出现在 linked 里。"""
        first = self._atom("/r", claim="甲经验", path="src/a.py")
        second = self._atom("/r", claim="乙经验", path="src/a.py")
        self.conn.execute("UPDATE atoms SET status='retired' WHERE id=?", (second,))
        self.conn.commit()
        link.rebuild(self.conn)
        self.assertNotIn((first, "same-file", second), self._edges())

    def test_doc_paths_are_not_linked(self):
        """几十条经验锚在同一份 CONFIGURATION.md 上，那是引用集中而不是代码相关。"""
        self._atom("/r", claim="甲经验", path="docs/CONFIGURATION.md")
        self._atom("/r", claim="乙经验", path="docs/CONFIGURATION.md")
        self.assertEqual(link.rebuild(self.conn)["edges"], 0)

    def test_commit_weight_counts_shared_commits_not_anchor_rows(self):
        """同一提交上各有多条锚点时，权重不能变成锚点数的乘积。"""
        first = db.add_atom(self.conn, {"claim": "甲经验", "repo": "/r", "commit_sha": "sha-1"})
        second = db.add_atom(self.conn, {"claim": "乙经验", "repo": "/r", "commit_sha": "sha-1"})
        for aid in (first, second):
            for path in ("src/a.py", "src/b.py", "src/c.py"):
                db.add_anchor(self.conn, aid, "file", path=path, commit_sha="sha-1")
            db.add_anchor(self.conn, aid, "commit", commit_sha="sha-1", excerpt=aid)
        link.rebuild(self.conn)
        self.assertEqual(self._edges()[(first, "same-commit", second)], 1.0)

    def test_search_exposes_linked_atoms(self):
        first = self._atom("/r", claim="甲经验", path="src/a.py")
        second = self._atom("/r", claim="乙经验", path="src/a.py")
        link.rebuild(self.conn)
        hits = search.search_atoms(self.conn, "甲经验", fallback=False, expand=True)
        self.assertEqual(hits[0]["id"], first)
        self.assertEqual(hits[0]["linked"][0]["id"], second)
        self.assertEqual(hits[0]["linked"][0]["claim"], "乙经验")
        self.assertEqual(hits[0]["linked"][0]["via"], ["src/a.py"])


class RefineTests(BrainTestBase):
    def test_retire_guard_only_only_touches_git_signal_guard_claims(self):
        guard = db.add_atom(self.conn, {"claim": "新增了「timeout」防护", "derivation": "git-signal"})
        manual = db.add_atom(self.conn, {"claim": "新增了「timeout」防护", "derivation": "session"})
        other = db.add_atom(self.conn, {"claim": "缺陷修复同时补充了测试：x", "derivation": "git-signal"})
        out = refine.retire_guard_only(self.conn)
        self.assertEqual(out["retired"], 1)
        self.assertEqual(db.get_atom(self.conn, guard)["status"], "retired")
        self.assertEqual(db.get_atom(self.conn, manual)["status"], "candidate")
        self.assertEqual(db.get_atom(self.conn, other)["status"], "candidate")
        self.assertEqual(out["active_remaining"], 2)

    def test_clear_clues(self):
        db.add_clue(self.conn, "r", "a.py", "timeout", "x", "sha")
        self.assertEqual(refine.clear_clues(self.conn)["clues_deleted"], 1)
        self.assertEqual(db.stats(self.conn)["clues"], 0)


class IngestRulesTests(BrainTestBase):
    def test_path_only_anchor_with_missing_file_is_unresolved(self):
        repo = Path(self.tmp.name) / "p"
        repo.mkdir()
        result = ingest.record(self.conn, {"claim": "路径锚点未必都能解析", "repo": str(repo),
                                           "path": "ghost.py"})
        self.assertEqual(result["anchors_resolved"], "0/1")
        self.assertIsNotNone(result["note"])  # 非 git 仓库，只能做文件校验

    def test_same_claim_in_two_repos_is_not_duplicate(self):
        payload = {"claim": "回调无幂等键会重复扣款", "path": "a.py"}
        self.assertNotIn("error", ingest.record(self.conn, {**payload, "repo": "r1"}))
        second = ingest.record(self.conn, {**payload, "repo": "r2"})
        self.assertFalse(second.get("duplicate", False))


class MineSignalTests(BrainTestBase):
    def test_claim_for_signal_branches(self):
        self.assertIn("回滚", mine._claim_for("x", ["revert"], False)[0])
        self.assertIn("紧急", mine._claim_for("x", ["hotfix"], False)[0])
        self.assertIn("测试", mine._claim_for("x", ["fix"], True)[0])
        self.assertIn("性能", mine._claim_for("x", ["perf"], False)[0])
        # fix 但没新增测试：不够硬，不升格
        self.assertEqual(mine._claim_for("x", ["fix"], False)[0], "")

    @unittest.skipIf(shutil.which("git") is None, "git 不可用")
    def test_noise_commits_are_filtered_before_signals(self):
        self.assertTrue(mine.is_noise("fix typo in readme", ["README.md"]))
        self.assertTrue(mine.is_noise("style: prettier", ["src/a.py"]))
        self.assertTrue(mine.is_noise("chore(deps): bump axios from 1 to 2", ["package.json"]))
        self.assertTrue(mine.is_noise("chore: update lock", ["pnpm-lock.yaml"]))
        self.assertTrue(mine.is_noise("wip", ["src/a.py"]))
        self.assertFalse(mine.is_noise("fix: retry without backoff", ["src/pay.py"]))
        self.assertEqual(mine.commit_kind('Revert "feat: retry"'), "revert")
        self.assertEqual(mine.commit_kind("hotfix: payment timeout"), "hotfix")
        self.assertEqual(mine.commit_kind("refactor: split scheduler"), "refactor")

    @unittest.skipIf(shutil.which("git") is None, "git 不可用")
    def test_revert_becomes_dead_end_not_claim(self):
        repo = Path(self.tmp.name) / "rev"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "a.py").write_text("v = 0\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")
        (repo / "a.py").write_text("v = 1\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", 'Revert "feat: add retry"')
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, encoding="utf-8").stdout.strip()
        mine.mine_repo(self.conn, str(repo), limit=10)
        dead = db.dead_ends_for(self.conn, path="a.py")
        self.assertTrue(any("Revert" in d["tried"] for d in dead))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM atoms WHERE commit_sha=?", (sha,)).fetchone()[0], 0)

    @unittest.skipIf(shutil.which("git") is None, "git 不可用")
    def test_churn_ignores_lock_docs_and_test_files(self):
        repo = Path(self.tmp.name) / "c2"
        repo.mkdir()
        _git(repo, "init", "-q")
        for name in ("pnpm-lock.yaml", "docs/guide.md", "a.spec.ts"):
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("v = 0\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "chore: init")
        for i in range(1, 4):
            for name in ("pnpm-lock.yaml", "docs/guide.md", "a.spec.ts"):
                (repo / name).write_text(f"v = {i}\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", f"chore: bump {i}")
        result = mine.mine_repo(self.conn, str(repo), limit=10)
        self.assertEqual(result["churn_created"], 0)

    @unittest.skipIf(shutil.which("git") is None, "git 不可用")
    def test_churn_atom_created_after_three_touches(self):
        repo = Path(self.tmp.name) / "c"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "hot.py").write_text("v = 0\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "chore: init")
        for i in range(1, 4):
            (repo / "hot.py").write_text(f"v = {i}\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", f"chore: touch {i}")
        result = mine.mine_repo(self.conn, str(repo), limit=10)
        self.assertGreaterEqual(result["churn_created"], 1)
        claims = [r["claim"] for r in self.conn.execute(
            "SELECT claim FROM atoms WHERE derivation='churn'")]
        self.assertTrue(any("hot.py" in c for c in claims))


class MinePrTests(BrainTestBase):
    """只收「评论 + 后续提交改了同一文件」的配对，且不落评论全文与 token。"""

    def _repo(self) -> Path:
        repo = Path(self.tmp.name) / "pr-repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "remote", "add", "origin", "https://github.com/acme/demo.git")
        return repo

    def _fetch(self, url: str):
        if url.endswith("/pulls?state=all&sort=updated&direction=desc&per_page=5"):
            return [{"number": 7}]
        if "/pulls/7/comments" in url:
            return [
                {"id": 1, "path": "src/a.py", "body": "这里应该加超时，否则会挂住",
                 "created_at": "2026-01-01T00:00:00Z"},
                {"id": 2, "path": "src/b.py", "body": "这段文档措辞可以再改改",
                 "created_at": "2026-01-01T00:00:00Z"},
            ]
        if "/pulls/7/commits" in url:
            return [{"sha": "abc123", "commit": {"author": {"date": "2026-01-02T00:00:00Z"}}}]
        if url.endswith("/commits/abc123"):
            return {"files": [{"filename": "src/a.py"}]}
        raise AssertionError(f"unexpected url: {url}")

    def test_only_pairs_whose_file_changed_afterwards(self):
        from brain import mine_pr
        repo = self._repo()
        result = mine_pr.mine(self.conn, str(repo), token="t", fetch=self._fetch)
        self.assertEqual(result["created"], 1)
        pair = result["pairs"][0]
        self.assertEqual(pair["path"], "src/a.py")
        self.assertNotIn("b.py", json.dumps(result, ensure_ascii=False))
        row = self.conn.execute(
            "SELECT * FROM atoms WHERE derivation='pr-comment'").fetchone()
        self.assertEqual(row["commit_sha"], "abc123")
        self.assertIn("超时", row["claim"])
        anchors = list(self.conn.execute(
            "SELECT kind,path FROM anchors WHERE atom_id=?", (row["id"],)))
        self.assertEqual({(a["kind"], a["path"]) for a in anchors},
                         {("commit", ""), ("file", "src/a.py")})

    def test_dry_run_reports_without_writing(self):
        from brain import mine_pr
        repo = self._repo()
        result = mine_pr.mine(self.conn, str(repo), token="t", dry_run=True,
                              fetch=self._fetch)
        self.assertEqual(result["created"], 0)
        self.assertTrue(result["pairs"][0]["dry_run"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0], 0)

    def test_requires_read_only_token_without_persisting_it(self):
        from brain import mine_pr
        repo = self._repo()
        result = mine_pr.mine(self.conn, str(repo), token="")
        self.assertIn("GITHUB_TOKEN", result["error"])
        # 错误信息里不能带出鉴权头或 token 本身
        self.assertNotIn("Bearer", json.dumps(result))


class _Resp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class LlmTests(BrainTestBase):
    def setUp(self):
        super().setUp()
        os.environ["DEEPSEEK_API_KEY"] = "sk-test"
        self.addCleanup(os.environ.pop, "DEEPSEEK_API_KEY", None)

    def _patch_urlopen(self, fn) -> None:
        from brain import llm
        patcher = mock.patch.object(llm.urllib.request, "urlopen", fn)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _no_sleep(self) -> None:
        from brain import llm
        patcher = mock.patch.object(llm.time, "sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_api_key_prefers_env_then_file_then_raises(self):
        from brain import llm
        os.environ.pop("DEEPSEEK_API_KEY", None)
        with self.assertRaises(llm.LLMError):
            llm.api_key()
        secrets = Path(self.tmp.name) / "secrets"
        secrets.mkdir()
        (secrets / "deepseek.key").write_text("sk-file\n", encoding="utf-8")
        self.assertEqual(llm.api_key(), "sk-file")
        os.environ["DEEPSEEK_API_KEY"] = "sk-env"
        self.assertEqual(llm.api_key(), "sk-env")

    def test_chat_json_returns_parsed_object(self):
        from brain import llm
        self._patch_urlopen(lambda req, timeout: _Resp(
            {"choices": [{"message": {"content": '{"claim": "ok"}'}}]}))
        self.assertEqual(llm.chat_json([{"role": "user", "content": "x"}], retries=0),
                         {"claim": "ok"})

    def test_http_400_is_not_retried(self):
        from brain import llm
        calls = []

        def boom(req, timeout):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b'{"e":1}'))

        self._patch_urlopen(boom)
        self._no_sleep()
        with self.assertRaises(llm.LLMError):
            llm.chat_json([{"role": "user", "content": "x"}], retries=2)
        self.assertEqual(len(calls), 1)

    def test_transport_error_is_retried_until_exhausted(self):
        from brain import llm
        calls = []

        def boom(req, timeout):
            calls.append(1)
            raise urllib.error.URLError("refused")

        self._patch_urlopen(boom)
        self._no_sleep()
        with self.assertRaises(llm.LLMError):
            llm.chat_json([{"role": "user", "content": "x"}], retries=2)
        self.assertEqual(len(calls), 3)

    def test_invalid_json_content_raises_llm_error(self):
        from brain import llm
        self._patch_urlopen(lambda req, timeout: _Resp(
            {"choices": [{"message": {"content": "不是 json"}}]}))
        self._no_sleep()
        with self.assertRaises(llm.LLMError):
            llm.chat_json([{"role": "user", "content": "x"}], retries=0)


class ProbeLoaderTests(BrainTestBase):
    def test_invalid_json_is_skipped(self):
        (Path(self.tmp.name) / "probes" / "broken.json").write_text("{not json", encoding="utf-8")
        ids = {p["id"] for p in probes.load_probes()}
        self.assertNotIn("broken", ids)
        self.assertIn("http-without-timeout", ids)

    def test_home_probe_overrides_package_probe(self):
        (Path(self.tmp.name) / "probes" / "http-without-timeout.json").write_text(
            json.dumps({"id": "http-without-timeout", "severity": "low",
                        "added_any": ["(?i)curl"]}), encoding="utf-8")
        found = [p for p in probes.load_probes() if p["id"] == "http-without-timeout"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "low")

    def test_probe_without_added_any_is_ignored(self):
        (Path(self.tmp.name) / "probes" / "empty.json").write_text(
            json.dumps({"id": "empty", "message": "无触发条件"}), encoding="utf-8")
        self.assertNotIn("empty", {p["id"] for p in probes.load_probes()})


@unittest.skipIf(shutil.which("git") is None, "git 不可用")
class ReplayTests(BrainTestBase):
    """回放只检验「规则与它自己的证据自洽」，不检验规则有没有用。"""

    PROBE = {
        "id": "demo-broad-except",
        "added_any": [r"(?i)except\s+(Exception|BaseException)\s*(as\s+\w+)?\s*:"],
        "unless_any": [r"(?i)(logger\.(exception|error)|traceback)"],
    }

    def _repo(self, bad: str, good: str) -> tuple[Path, str]:
        repo = Path(self.tmp.name) / "replay-repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "svc.py").write_text(bad, encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")
        (repo / "svc.py").write_text(good, encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "fix: 拆分错误码")
        head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()
        return repo, head

    def _atom(self, repo: Path, sha: str, with_path: bool = True) -> str:
        aid = db.add_atom(self.conn, {"claim": "宽捕获把不同故障压成同一个错误码",
                                      "repo": str(repo), "commit_sha": sha})
        if with_path:
            db.add_anchor(self.conn, aid, "file", path="svc.py", commit_sha=sha)
        return aid

    def test_hits_the_bad_code_it_came_from(self):
        repo, sha = self._repo(
            "def f():\n    try:\n        run()\n    except Exception:\n        return None\n",
            "def f():\n    try:\n        run()\n    except ValueError:\n        raise\n",
        )
        result = replay.replay_one(self.conn, {**self.PROBE, "from_atom": self._atom(repo, sha)})
        self.assertEqual(result["status"], "hit")
        self.assertEqual(result["path"], "svc.py")
        self.assertEqual(result["line"], 4)

    def test_miss_reports_the_gate_that_blocked(self):
        repo, sha = self._repo(
            "def f():\n    return 1\n",
            "def f():\n    try:\n        run()\n    except ValueError:\n        raise\n",
        )
        result = replay.replay_one(self.conn, {**self.PROBE, "from_atom": self._atom(repo, sha)})
        self.assertEqual(result["status"], "miss")
        self.assertEqual(result["blocked_by"], "added_any")

    def test_unless_any_exempts_a_file_that_already_logs(self):
        repo, sha = self._repo(
            "def f():\n    try:\n        run()\n    except Exception as exc:\n"
            "        logger.error('boom', exc_info=exc)\n",
            "def f():\n    try:\n        run()\n    except ValueError:\n        raise\n",
        )
        result = replay.replay_one(self.conn, {**self.PROBE, "from_atom": self._atom(repo, sha)})
        self.assertEqual(result["status"], "miss")
        self.assertEqual(result["blocked_by"], "unless_any")

    def test_anchorless_states_are_distinguished(self):
        repo, sha = self._repo("x = 1\n", "x = 2\n")
        no_commit = replay.replay_one(self.conn, {**self.PROBE, "from_atom": self._atom(repo, "")})
        self.assertEqual(no_commit["status"], "no_commit")
        no_path = replay.replay_one(
            self.conn, {**self.PROBE, "from_atom": self._atom(repo, sha, with_path=False)})
        self.assertEqual(no_path["status"], "no_path_anchor")
        no_source = replay.replay_one(self.conn, {**self.PROBE, "from_atom": "a-missing"})
        self.assertEqual(no_source["status"], "no_source")

    def test_first_commit_has_no_parent(self):
        repo = Path(self.tmp.name) / "solo"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "svc.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, encoding="utf-8").stdout.strip()
        result = replay.replay_one(self.conn, {**self.PROBE, "from_atom": self._atom(repo, sha)})
        self.assertEqual(result["status"], "no_parent")

    def test_replay_all_skips_probes_without_provenance(self):
        repo, sha = self._repo(
            "def f():\n    try:\n        run()\n    except Exception:\n        return None\n",
            "def f():\n    try:\n        run()\n    except ValueError:\n        raise\n",
        )
        linked = {**self.PROBE, "from_atom": self._atom(repo, sha)}
        loose = {**self.PROBE, "id": "demo-without-provenance"}
        with mock.patch.object(replay.probes, "load_probes", return_value=[linked, loose]):
            report = replay.replay_all(self.conn)
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["by_status"], {"hit": 1})
        self.assertEqual(report["hit_rate"], 1.0)
        self.assertEqual(report["unrunnable"], 0)

    def test_replay_false_is_an_explicit_exemption(self):
        """仓库历史被压成单个提交时没有父版本可回放；保留溯源但不算命中率。"""
        exempt = {**self.PROBE, "from_atom": "a-1", "replay": False}
        with mock.patch.object(replay.probes, "load_probes", return_value=[exempt]):
            report = replay.replay_all(self.conn)
        self.assertEqual(report["total"], 0)
        self.assertIsNone(report["hit_rate"])

    def test_hit_rate_excludes_data_gaps_from_denominator(self):
        """no_commit 是数据缺口，不是规则不自洽；算进分母会让旧库永远过不了门禁。"""
        hit = {"probe": "p-hit", "atom": "a-1", "status": "hit"}
        miss = {"probe": "p-miss", "atom": "a-2", "status": "miss", "blocked_by": "added_any"}
        gap = {"probe": "p-gap", "atom": "a-3", "status": "no_commit"}
        with mock.patch.object(replay, "replay_one", side_effect=[hit, miss, gap]):
            with mock.patch.object(replay.probes, "load_probes", return_value=[
                {"id": "p-hit", "from_atom": "a-1"},
                {"id": "p-miss", "from_atom": "a-2"},
                {"id": "p-gap", "from_atom": "a-3"},
            ]):
                report = replay.replay_all(self.conn)
        self.assertEqual(report["hit_rate"], 0.5)
        self.assertEqual(report["unrunnable"], 1)
        self.assertEqual(report["misses"], ["p-miss"])

    def test_record_snapshot_is_read_by_funnel(self):
        """funnel 不该为了看指标再跑一遍 git：回放结果落快照，funnel 只读快照。"""
        snapshot = replay.record({"total": 2, "by_status": {"hit": 2}, "blocked_by": {},
                                  "hit_rate": 1.0, "unrunnable": 0, "misses": [],
                                  "rows": [{"probe": "p"}]})
        self.assertIn("ran_at", snapshot)
        report = funnel.report(self.conn)
        self.assertEqual(report["probes"]["hit_rate"], 1.0)
        self.assertNotIn("rows", report["probes"])


@unittest.skipIf(shutil.which("git") is None, "git 不可用")
class HookTests(BrainTestBase):
    """hook 只入队，不挖矿：提交路径上不能跑 git diff 与模型。"""

    def _repo(self) -> tuple[Path, str]:
        repo = Path(self.tmp.name) / "hooked"
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "init")
        (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", 'Revert "feat: add handler"')
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, encoding="utf-8").stdout.strip()
        return repo, sha

    def _run(self, *argv: str) -> tuple[int, str]:
        from brain import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_install_preserves_existing_hook_and_uninstall_restores_it(self):
        from brain import hooks
        repo, _ = self._repo()
        hooks_dir = Path(repo) / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        custom = hooks_dir / "post-commit"
        original = "#!/bin/sh\necho custom\n"
        custom.write_text(original, encoding="utf-8")

        result = hooks.install(str(repo))
        self.assertEqual(result["installed"], ["post-commit", "post-merge"])
        self.assertIn("# agentbrain-hook: 1", custom.read_text(encoding="utf-8"))
        self.assertEqual((hooks_dir / "post-commit.pre-brain").read_text(encoding="utf-8"),
                         original)
        # 重装不能把备份覆盖成我们自己
        hooks.install(str(repo))
        self.assertEqual((hooks_dir / "post-commit.pre-brain").read_text(encoding="utf-8"),
                         original)

        hooks.uninstall(str(repo))
        self.assertEqual(custom.read_text(encoding="utf-8"), original)
        self.assertFalse((hooks_dir / "post-commit.pre-brain").exists())

    def test_pre_commit_gate_is_opt_in(self):
        """pre-commit 会阻塞提交，所以只有显式要求时才装。"""
        from brain import hooks
        repo, _ = self._repo()
        hooks_dir = Path(repo) / ".git" / "hooks"
        self.assertEqual(hooks.install(str(repo))["installed"],
                         ["post-commit", "post-merge"])
        self.assertFalse((hooks_dir / "pre-commit").exists())

        result = hooks.install(str(repo), pre_commit=True)
        self.assertIn("pre-commit", result["installed"])
        script = (hooks_dir / "pre-commit").read_text(encoding="utf-8")
        self.assertIn("--min-hit-rate", script)
        self.assertIn("PYTHONPATH=", script)
        hooks.uninstall(str(repo))
        self.assertFalse((hooks_dir / "pre-commit").exists())

    def test_queue_dedupes_and_consume_removes_processed(self):
        from brain import hooks
        path = hooks.queue_path()
        path.write_text(
            "2026-11-02T10:00:00Z\tpost-commit\tabc123\tE:/proj-a\n"
            "2026-11-02T10:00:00Z\tpost-commit\tabc123\tE:/proj-a\n"
            "bad line\n",
            encoding="utf-8")
        entries = hooks.read_queue()
        self.assertEqual(len(entries), 1)
        # 物理上的重复行一起清掉，避免下次再从同一提交挖一遍
        self.assertEqual(hooks.consume(entries), 2)
        self.assertEqual(hooks.read_queue(), [])

    def test_mine_from_queue_processes_only_queued_commits(self):
        from brain import hooks
        repo, sha = self._repo()
        hooks.queue_path().write_text(
            f"2026-11-02T10:00:00Z\tpost-commit\t{sha}\t{repo.as_posix()}\n",
            encoding="utf-8")
        code, out = self._run("mine", "--from-queue")
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["consumed"], 1)
        self.assertGreaterEqual(result["results"][0]["created"], 1)
        self.assertEqual(hooks.read_queue(), [])


class CliTests(BrainTestBase):
    def _run(self, *argv: str) -> tuple[int, str]:
        from brain import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(list(argv))
        return code, buf.getvalue()

    def test_add_source_then_stats_lists_it(self):
        code, out = self._run("add-source", self.tmp.name)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["sources"], [str(Path(self.tmp.name).resolve())])

    def test_replay_gate_fails_when_hit_rate_below_threshold(self):
        """回放门禁靠退出码表达：CI 与 pre-commit 都只看这个码。"""
        import contextlib as _ctx
        from brain import cli
        low = {"total": 2, "by_status": {"hit": 1, "miss": 1}, "blocked_by": {},
               "hit_rate": 0.5, "unrunnable": 0, "misses": ["p"], "rows": []}
        with mock.patch.object(replay, "replay_all", return_value=low):
            buf = io.StringIO()
            with _ctx.redirect_stdout(buf):
                code = cli.main(["replay", "--min-hit-rate", "0.9"])
        self.assertEqual(code, 2)
        self.assertIn("门禁未通过", buf.getvalue())

    def test_add_with_anchor_succeeds_and_without_anchor_fails(self):
        code, out = self._run("add", "--claim", "并发写入缺少唯一键会导致重复扣款",
                              "--path", "a.py", "--repo", "r")
        self.assertEqual(code, 0)
        self.assertIn("atom_id", json.loads(out))
        code, out = self._run("add", "--claim", "并发写入缺少唯一键会导致重复扣款")
        self.assertEqual(code, 2)

    def test_outcome_records_real_feedback(self):
        aid = db.add_atom(self.conn, {"claim": "回调在无幂等键时会重复扣款", "confidence": 0.5})
        code, out = self._run("outcome", aid, "validated", "--note", "本会话实测")
        self.assertEqual(code, 0)
        self.assertAlmostEqual(json.loads(out)["confidence"], 0.65, places=2)

    def test_search_reports_no_hit_instead_of_fallback(self):
        db.add_atom(self.conn, {"claim": "唯一的高置信条目", "confidence": 0.9})
        code, out = self._run("search", "zzzqqq")
        self.assertEqual(code, 0)
        self.assertIn("无命中", out)

    def test_explain_missing_atom_exits_2(self):
        code, out = self._run("explain", "a-doesnotexist")
        self.assertEqual(code, 2)
        self.assertIn("atom 不存在", json.loads(out)["error"])

    def test_rate_good_requires_resolved_anchor(self):
        aid = db.add_atom(self.conn, {"claim": "x", "repo": "r"})
        db.add_anchor(self.conn, aid, "file", path="ghost.py")
        code, out = self._run("rate", aid, "good")
        self.assertEqual(code, 3)
        self.assertIn("锚点尚未解析", json.loads(out)["error"])

    def test_rate_good_accepts_and_bad_retires(self):
        repo = Path(self.tmp.name) / "w"
        repo.mkdir()
        (repo / "a.py").write_text("lock = acquire()\n", encoding="utf-8")
        good = db.add_atom(self.conn, {"claim": "x", "repo": str(repo)})
        db.add_anchor(self.conn, good, "file", path="a.py", excerpt="lock = acquire()")
        verify.verify_all(self.conn)
        code, _ = self._run("rate", good, "good")
        self.assertEqual(code, 0)
        self.assertEqual(db.get_atom(self.conn, good)["status"], "accepted")

        bad = db.add_atom(self.conn, {"claim": "y", "repo": str(repo)})
        db.add_anchor(self.conn, bad, "file", path="ghost.py")
        code, _ = self._run("rate", bad, "bad")
        self.assertEqual(code, 0)
        self.assertEqual(db.get_atom(self.conn, bad)["status"], "retired")

    def test_link_reports_edge_count_and_is_repeatable(self):
        for claim in ("甲经验", "乙经验"):
            aid = db.add_atom(self.conn, {"claim": claim, "repo": "/r"})
            db.add_anchor(self.conn, aid, "file", path="src/a.py")
        code, out = self._run("link")
        self.assertEqual(code, 0)
        first = json.loads(out)["edges"]
        self.assertEqual(first, 2)
        code, out = self._run("link")
        self.assertEqual(json.loads(out)["edges"], first)

    def test_revisions_lists_old_and_new_then_revert_restores(self):
        aid = db.add_atom(self.conn, {"claim": "新陈述", "conditions": "新条件"})
        db.save_revision(self.conn, aid,
                         {"claim": "旧陈述", "conditions": "旧条件"},
                         {"claim": "新陈述", "conditions": "新条件"}, "m", "accepted")
        rid = self.conn.execute("SELECT id FROM revisions").fetchone()[0]
        code, out = self._run("revisions")
        self.assertEqual(code, 0)
        self.assertIn("旧陈述", out)
        self.assertIn("新条件", out)
        code, _ = self._run("revert", str(rid))
        self.assertEqual(code, 0)
        self.assertEqual(db.get_atom(self.conn, aid)["claim"], "旧陈述")

    def test_funnel_reports_four_layers(self):
        db.add_atom(self.conn, {"claim": "条目"})
        code, out = self._run("funnel")
        self.assertEqual(code, 0)
        self.assertIn("知识层", out)
        self.assertIn("反馈层", out)
        self.assertIn("真实使用反馈覆盖", out)

    def test_triage_reports_counts_without_touching_status(self):
        aid = db.add_atom(self.conn, {
            "claim": "在消费端多线程且无唯一索引时，回调重复执行会导致同一订单被扣款两次"})
        db.add_anchor(self.conn, aid, "file", path="ghost.py")
        code, out = self._run("triage")
        self.assertEqual(code, 0)
        self.assertIn("退休 1", out)
        self.assertIn("--apply", out)
        self.assertEqual(db.get_atom(self.conn, aid)["status"], "candidate")

    def test_prune_needs_a_flag_and_retires_guard_only(self):
        code, out = self._run("prune")
        self.assertEqual(code, 2)
        self.assertIn("error", json.loads(out))

        aid = db.add_atom(self.conn, {"claim": "新增了「timeout」防护", "derivation": "git-signal"})
        code, out = self._run("prune", "--guard-only")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["retired"], 1)
        self.assertEqual(db.get_atom(self.conn, aid)["status"], "retired")

    def test_clues_lists_empty_state(self):
        code, out = self._run("clues")
        self.assertEqual(code, 0)
        self.assertIn("暂无探针线索", out)

    def test_mine_without_sources_exits_2(self):
        code, out = self._run("mine")
        self.assertEqual(code, 2)
        self.assertIn("没有种子仓库", json.loads(out)["error"])

    def test_review_reports_empty_state(self):
        code, out = self._run("review")
        self.assertEqual(code, 0)
        self.assertIn("没有待审候选", out)

    def test_check_json_reports_findings(self):
        diff = (Path(self.tmp.name) / "d.diff")
        diff.write_text("--- a/src/a.py\n+++ b/src/a.py\n@@ -0,0 +1,1 @@\n"
                        "+resp = fetch('https://api.example.com/v1/x')\n", encoding="utf-8")
        code, out = self._run("check", "--diff", str(diff), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["findings"][0]["probe"], "http-without-timeout")

    def test_mcp_config_shape(self):
        code, out = self._run("mcp-config")
        self.assertEqual(code, 0)
        server = json.loads(out)["mcpServers"]["agentbrain"]
        self.assertEqual(server["args"], ["-m", "brain", "mcp"])
        self.assertEqual(server["env"]["BRAIN_HOME"], self.tmp.name)

    def test_enrich_exit_code_follows_error_count(self):
        with mock.patch.object(enrich, "run", return_value={
                "selected": 1, "accepted": 1, "rejected": 0, "skipped": 0,
                "errors": 0, "accept_rate": 1.0}):
            code, out = self._run("enrich", "--sleep", "0")
        self.assertEqual(code, 0)
        self.assertIn("接受 1", out)
        with mock.patch.object(enrich, "run", return_value={
                "selected": 1, "accepted": 0, "rejected": 0, "skipped": 0,
                "errors": 1, "accept_rate": 0.0}):
            code, _ = self._run("enrich", "--sleep", "0")
        self.assertEqual(code, 1)


class McpMoreTests(BrainTestBase):
    def _call(self, name: str, args: dict) -> dict:
        from brain import mcp
        resp = mcp.handle(self.conn, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": name, "arguments": args}})
        return json.loads(resp["result"]["content"][0]["text"])

    def test_tools_list_exposes_eleven_tools(self):
        from brain import mcp
        resp = mcp.handle(self.conn, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(names, {"search", "explain", "record_atom", "record_dead_end",
                                 "check_diff", "audit_diff", "record_outcome", "revoke_outcome",
                                 "review_atom", "review_pending", "stats"})

    def test_initialize_carries_instructions(self):
        from brain import mcp
        resp = mcp.handle(self.conn, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        text = resp["result"]["instructions"]
        self.assertIn("何时用", text)
        self.assertIn("零命中", text)  # 必须说明空结果的含义，否则模型会误判为"无风险"

    def test_every_tool_description_states_when_to_use(self):
        from brain import mcp
        for tool in mcp.TOOLS:
            self.assertTrue(tool["description"].startswith("何时用"),
                            f"{tool['name']} 的描述没有写使用时机")

    def test_review_atom_accepts_and_records_outcome(self):
        repo = Path(self.tmp.name) / "rv"
        repo.mkdir()
        (repo / "a.py").write_text("lock = acquire()\n", encoding="utf-8")
        aid = db.add_atom(self.conn, {"claim": "x", "repo": str(repo)})
        db.add_anchor(self.conn, aid, "file", path="a.py", excerpt="lock = acquire()")
        verify.verify_all(self.conn)
        out = self._call("review_atom", {"atom_id": aid, "verdict": "good", "note": "本轮验证有效"})
        self.assertEqual(out["status"], "accepted")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0], 1)

    def test_review_atom_bad_retires(self):
        aid = db.add_atom(self.conn, {"claim": "x"})
        out = self._call("review_atom", {"atom_id": aid, "verdict": "bad"})
        self.assertEqual(out["status"], "retired")

    def test_review_atom_blocks_unresolved_anchor(self):
        aid = db.add_atom(self.conn, {"claim": "x", "repo": "r"})
        db.add_anchor(self.conn, aid, "file", path="ghost.py")
        out = self._call("review_atom", {"atom_id": aid, "verdict": "good"})
        self.assertIn("锚点尚未解析", out["error"])
        self.assertEqual(db.get_atom(self.conn, aid)["status"], "candidate")

    def test_review_atom_rejects_unknown_verdict_and_missing_atom(self):
        self.assertIn("error", self._call("review_atom", {"atom_id": "a-x", "verdict": "maybe"}))
        self.assertIn("error", self._call("review_atom", {"atom_id": "a-x", "verdict": "good"}))

    def test_notification_is_ignored_and_not_logged(self):
        from brain import mcp
        self.assertIsNone(mcp.handle(self.conn, {
            "jsonrpc": "2.0", "method": "tools/call", "params": {"name": "stats", "arguments": {}}}))
        self.assertFalse((Path(self.tmp.name) / "logs" / "mcp-calls.jsonl").exists())

    def test_unknown_tool_reports_error_text(self):
        self.assertIn("未知工具", self._call("nope", {})["error"])

    def test_unknown_method_returns_jsonrpc_error(self):
        from brain import mcp
        resp = mcp.handle(self.conn, {"jsonrpc": "2.0", "id": 9, "method": "resources/list"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_record_outcome_validates_enum_and_atom_existence(self):
        self.assertIn("error", self._call("record_outcome", {"atom_id": "a-x", "outcome": "maybe"}))
        self.assertIn("error", self._call("record_outcome", {"atom_id": "a-x", "outcome": "validated"}))

    def test_explain_missing_atom_reports_error(self):
        self.assertIn("error", self._call("explain", {"atom_id": "a-x"}))

    def test_audit_diff_passes_record_flag(self):
        from brain import mcp
        with mock.patch.object(mcp.audit, "run", return_value={"candidates": 0, "results": []}) as run:
            mcp.handle(self.conn, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                   "params": {"name": "audit_diff",
                                              "arguments": {"diff_text": "x", "record": False}}})
        self.assertFalse(run.call_args.kwargs["record"])

    def test_tool_exception_is_returned_as_error_and_logged(self):
        from brain import mcp
        resp = mcp.handle(self.conn, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "check_diff",
                                                 "arguments": {"diff_text": None}}})
        # 单条工具失败返回 isError，不把整个 JSON-RPC 调用打成协议错误
        self.assertTrue(resp["result"]["isError"])
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertIn("check_diff", payload["tool"])
        row = json.loads((Path(self.tmp.name) / "logs" / "mcp-calls.jsonl")
                         .read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(row["tool"], "check_diff")
        self.assertFalse(row["ok"])


if __name__ == "__main__":
    unittest.main()
