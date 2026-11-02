"""写入一条经验。CLI 与 MCP 共用，保证两条入口行为一致。

硬约束（与设计一致）：
- claim 必须是一句可证伪的陈述，禁止"注意并发"这类空话；
- 至少一个锚点（commit 或 path），否则无法追溯，直接拒绝；
- 写入后立刻做锚点机检，把结果回给调用方，而不是等到 review 才发现是空的。
"""
from __future__ import annotations

from . import db, gitutil, verify

MIN_CLAIM = 8


def record(conn, payload: dict) -> dict:
    claim = (payload.get("claim") or "").strip()
    if len(claim) < MIN_CLAIM:
        return {"error": f"claim 需为一句可证伪的陈述（至少 {MIN_CLAIM} 字）"}

    repo = (payload.get("repo") or "").strip()
    sources = db.stats(conn)["sources"]
    if not repo:
        repo = sources[0] if sources else ""

    commit = (payload.get("commit") or "").strip()
    path = (payload.get("path") or "").strip()
    if not commit and not path:
        return {"error": "至少需要一个锚点：commit 或 path"}

    dup = conn.execute(
        "SELECT id FROM atoms WHERE repo=? AND claim=? LIMIT 1", (repo, claim)
    ).fetchone()
    if dup is not None:
        return {"atom_id": dup["id"], "duplicate": True, "status": "已有同名陈述，未重复写入"}

    try:
        confidence = float(payload.get("confidence", 0.35))
    except (TypeError, ValueError):
        confidence = 0.35

    aid = db.add_atom(conn, {
        "claim": claim,
        "mechanism": (payload.get("mechanism") or "").strip(),
        "conditions": (payload.get("conditions") or "").strip(),
        "counter": (payload.get("counter") or "").strip(),
        "derivation": (payload.get("derivation") or "session").strip(),
        "confidence": confidence,
        "repo": repo,
        "commit_sha": commit,
    })

    excerpt = (payload.get("excerpt") or "").strip()
    if commit:
        db.add_anchor(conn, aid, "commit", commit_sha=commit, excerpt=excerpt or claim)
    if path:
        db.add_anchor(conn, aid, "file", path=path, commit_sha=commit,
                      line_start=payload.get("line_start"),
                      line_end=payload.get("line_end"),
                      excerpt=excerpt)

    row = db.get_atom(conn, aid)
    resolved, total = verify.verify_atom(conn, row)
    note = None
    if repo and not gitutil.is_repo(repo):
        note = "repo 不是 git 仓库，仅 path 锚点可做文件校验"
    return {
        "atom_id": aid,
        "status": row["status"],
        "repo": repo,
        "anchors_resolved": f"{resolved}/{total}",
        "note": note,
        "next": "等待 review：good 采纳（MCP review_atom / CLI brain rate），bad 退休",
    }


def feedback(conn, atom_id: str, outcome: str, note: str = "") -> dict:
    """记一次任务里的真实反馈。MCP 与 CLI 共用，避免两条入口的词表与计分口径漂移。

    只有 validated / insufficient 两种：前者是"这条经验确实解释了问题"，
    后者是"这次没用上或误导了人"。机器判定（triage / audit）不走这里。
    """
    if outcome not in ("validated", "insufficient"):
        return {"error": "outcome 必须是 validated 或 insufficient", "reason": "bad_outcome"}
    if db.get_atom(conn, atom_id) is None:
        return {"error": "atom 不存在", "reason": "missing"}
    db.record_outcome(conn, atom_id, outcome, note)
    row = db.get_atom(conn, atom_id)
    return {"atom_id": atom_id, "confidence": row["confidence"], "status": row["status"]}


def review(conn, atom_id: str, verdict: str, note: str = "") -> dict:
    """人工两键：good 采纳 / bad 退休。CLI 与 MCP 共用，两条入口行为必须一致。

    good 之前要求至少一条锚点已解析：否则转正的是一条无法回溯的条目。
    """
    if verdict not in ("good", "bad"):
        return {"error": "verdict 必须为 good 或 bad", "reason": "bad_verdict"}
    row = db.get_atom(conn, atom_id)
    if row is None:
        return {"error": "atom 不存在", "reason": "missing"}

    if verdict == "good":
        if row["status"] == "accepted":
            return {"atom_id": atom_id, "status": "accepted", "note": "已是 accepted，未重复记分"}
        anchors = db.anchors_of(conn, atom_id)
        if anchors and not any(a["resolved"] == 1 for a in anchors):
            return {"error": "锚点尚未解析，先运行 brain verify",
                    "reason": "unresolved_anchor", "atom_id": atom_id}
        conn.execute("UPDATE atoms SET status='accepted', updated_at=? WHERE id=?",
                     (db.now(), atom_id))
        conn.commit()
        db.record_outcome(conn, atom_id, "validated", note or "manual-review")
    else:
        conn.execute("UPDATE atoms SET status='retired', updated_at=? WHERE id=?",
                     (db.now(), atom_id))
        conn.commit()
        db.record_outcome(conn, atom_id, "insufficient", note or "manual-review")

    updated = db.get_atom(conn, atom_id)
    return {"atom_id": atom_id, "status": updated["status"],
            "confidence": updated["confidence"]}
