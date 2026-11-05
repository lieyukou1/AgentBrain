"""回放：拿探针去认它自己来源的「坏代码」，检验规则是否自洽。

做法：找到经验锚定的修复提交，取其父提交里该文件的完整内容，当成"全新增"喂给探针。
命中 = 规则能识别这个坑；不命中 = 规则太窄，或这条经验压根不是那个坑。

为什么要整份文件而不是只回放被删的行：
只回放删除行会把 require_any 依赖的上下文一起丢掉（例如 update_one 那行没被改），
结果规则被自己的上下文门槛饿死，测出来的是"孤立行命中率"，没有参考价值。

这不是"有用性验证"，只是"自洽性验证"：证明规则和它的证据不矛盾。
"""
from __future__ import annotations

import json

from . import db, gitutil, probes


def parent_of(repo: str, sha: str) -> str | None:
    code, out = gitutil.git(repo, "rev-parse", "--verify", f"{sha}^")
    return out.strip() if code == 0 and out.strip() else None


def full_file_diff(repo: str, sha: str, path: str) -> str:
    """把某个提交里的整份文件包成「全新增」的 unified diff。"""
    text = gitutil.file_at(repo, sha, path)
    if text is None:
        return ""
    lines = text.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def replay_one(conn, probe: dict) -> dict:
    atom_id = probe.get("from_atom") or ""
    atom = db.get_atom(conn, atom_id) if atom_id else None
    base = {"probe": probe["id"], "atom": atom_id}
    if atom is None:
        return {**base, "status": "no_source"}
    repo, sha = atom["repo"], atom["commit_sha"]
    if not sha:
        return {**base, "status": "no_commit", "repo": repo}
    parent = parent_of(repo, sha)
    if parent is None:
        return {**base, "status": "no_parent", "commit": sha[:10]}
    paths = [a["path"] for a in db.anchors_of(conn, atom_id) if a["path"]]
    if not paths:
        return {**base, "status": "no_path_anchor", "commit": sha[:10]}

    blocked = "test_path"
    for path in paths:
        if probes.TEST_PATH.search(path) and not probe.get("include_tests"):
            continue
        diff = full_file_diff(repo, parent, path)
        if not diff:
            continue
        for scanned, added in probes.parse_diff(diff, code_only=True).items():
            verdict = probes.evaluate(probe, scanned, added)
            if verdict["hit"]:
                return {**base, "status": "hit", "commit": sha[:10],
                        "path": scanned, "line": verdict["line"]}
            # 第一个能走到后面的拦截原因最有诊断价值
            if verdict["blocked_by"] != "added_any" or blocked == "test_path":
                blocked = verdict["blocked_by"]
    return {**base, "status": "miss", "commit": sha[:10], "blocked_by": blocked}


def replay_all(conn, only_probe: str | None = None) -> dict:
    rows = []
    for probe in probes.load_probes():
        if only_probe and probe["id"] != only_probe:
            continue
        # replay=false 是显式豁免：来源仓库历史被压成单个初始提交、没有父版本可回放时，
        # 保留 from_atom 供 check 溯源，但不把它算进命中率分母。
        if not probe.get("from_atom") or probe.get("replay") is False:
            continue
        rows.append(replay_one(conn, probe))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    blocked: dict[str, int] = {}
    for row in rows:
        if row["status"] == "miss":
            blocked[row["blocked_by"]] = blocked.get(row["blocked_by"], 0) + 1
    # 命中率只算「真的跑起来了」的探针：no_source / no_commit 是数据缺口，
    # 不是规则不自洽；把它们算进分母会让一个没提交锚点的旧库永远过不了门禁。
    runnable = counts.get("hit", 0) + counts.get("miss", 0)
    return {
        "total": len(rows),
        "by_status": counts,
        "blocked_by": blocked,
        "hit_rate": round(counts.get("hit", 0) / runnable, 3) if runnable else None,
        "unrunnable": len(rows) - runnable,
        "misses": [r["probe"] for r in rows if r["status"] == "miss"],
        "rows": rows,
    }


def record_path():
    path = db.home() / "logs" / "replay.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def record(result: dict) -> dict:
    """落一份快照给 funnel 读：funnel 不该为了看指标再跑一遍 git。"""
    snapshot = {k: v for k, v in result.items() if k != "rows"}
    snapshot["ran_at"] = db.now()
    record_path().write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
    return snapshot


def last_record() -> dict | None:
    path = record_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
