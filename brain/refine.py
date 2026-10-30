"""提纯：把早期误升格的条目退回，不删除，保留可追溯历史。

方案 A：guard-only（"新增了什么防护"）本身不构成可证伪的知识，
它只是"这里可能有防护逻辑"的线索，应降级为 clues 表。
方案 B：triage 把候选按机检规则分档，人工只需要看真正说不准的那一部分。
"""
from __future__ import annotations

from . import db, ingest
from .enrich import GENERIC, PLACEHOLDER_CONDITIONS

GUARD_CLAIM_LIKE = "%新增了「%"
MIN_CLAIM, MAX_CLAIM = 15, 300
MIN_MECHANISM = 12
MIN_COUNTER = 8


def retire_guard_only(conn) -> dict:
    rows = list(conn.execute(
        "SELECT id, claim FROM atoms WHERE derivation='git-signal' AND claim LIKE ?"
        " AND status!='retired'", (GUARD_CLAIM_LIKE,)
    ))
    for row in rows:
        conn.execute(
            "UPDATE atoms SET status='retired', updated_at=? WHERE id=?", (db.now(), row["id"])
        )
        db.record_outcome(conn, row["id"], "insufficient", "auto-pruned: guard-only（方案A）")
    conn.commit()
    remaining = conn.execute("SELECT COUNT(*) FROM atoms WHERE status!='retired'").fetchone()[0]
    return {"retired": len(rows), "active_remaining": remaining}


def clear_clues(conn) -> dict:
    """清空待处理线索，用于在收紧过滤器后重新生成。"""
    n = conn.execute("DELETE FROM clues WHERE status='pending'").rowcount
    conn.commit()
    return {"clues_deleted": n}


def classify(conn, row) -> tuple[str, str]:
    """单条候选的机检结论：retire / accept / refill / hold，附一句可追溯的理由。

    只回答机器能判的问题（锚点、长度、字段是否为空、是否占位），
    不做语义判断——语义留给使用反馈，这也是 accepted 只该表示"证据齐全"的原因。
    """
    anchors = db.anchors_of(conn, row["id"])
    resolved = sum(1 for a in anchors if a["resolved"] == 1)
    claim = (row["claim"] or "").strip()
    mechanism = (row["mechanism"] or "").strip()
    conditions = (row["conditions"] or "").strip()
    counter = (row["counter"] or "").strip()

    if not anchors:
        return "retire", "无锚点"
    if resolved == 0:
        return "retire", "锚点全部未解析"
    if len(claim) < MIN_CLAIM:
        return "retire", "claim 过短"
    for word in GENERIC:
        if word in claim:
            return "retire", f"含泛化话术「{word}」"
    if not mechanism:
        return "retire", "无机制"
    if resolved < len(anchors):
        return "hold", f"锚点仅解析 {resolved}/{len(anchors)}"
    if len(claim) > MAX_CLAIM:
        return "hold", "claim 过长"
    if len(mechanism) < MIN_MECHANISM:
        return "hold", "机制过短"
    if len(counter) < MIN_COUNTER:
        return "hold", "缺少实质反例"
    if not conditions or any(p in conditions for p in PLACEHOLDER_CONDITIONS):
        return "refill", "conditions 是占位话术，需重新富化"
    return "accept", "机检通过"


def triage(conn, apply: bool = False) -> dict:
    """把候选分成四档；apply=True 时退休 retire、采纳 accept，其余不动。

    refill 单独成档是因为它"有救"：反例与机制都在，只是 conditions 字段被
    早期富化写成了占位话术，重跑一次富化就能补齐，不该直接退休。
    """
    buckets: dict[str, list] = {"retire": [], "accept": [], "refill": [], "hold": []}
    for row in conn.execute("SELECT * FROM atoms WHERE status='candidate'"):
        bucket, reason = classify(conn, row)
        buckets[bucket].append({"id": row["id"], "reason": reason, "claim": row["claim"]})

    if apply:
        for item in buckets["retire"]:
            ingest.review(conn, item["id"], "bad", f"triage: {item['reason']}")
        for item in buckets["accept"]:
            ingest.review(conn, item["id"], "good", "triage: 机检通过（未逐条人工复核）")

    return {
        "applied": apply,
        "counts": {name: len(items) for name, items in buckets.items()},
        "buckets": buckets,
    }


def revert_revision(conn, revision_id: int) -> dict:
    """把某次富化回退到 revisions 里留底的旧文本。

    富化是就地覆盖，revisions 是唯一的留底，所以回退只能走它；
    回退本身再记一条 revision，保证动作同样可追溯。
    """
    row = conn.execute("SELECT * FROM revisions WHERE id=?", (revision_id,)).fetchone()
    if row is None:
        return {"error": "revision 不存在", "reason": "missing"}
    current = db.get_atom(conn, row["atom_id"])
    if current is None:
        return {"error": "atom 不存在", "reason": "missing"}

    conn.execute(
        "UPDATE atoms SET claim=?, mechanism=?, conditions=?, counter=?, updated_at=?"
        " WHERE id=?",
        (row["old_claim"], row["old_mechanism"], row["old_conditions"],
         row["old_counter"], db.now(), row["atom_id"]),
    )
    conn.commit()
    db.save_revision(
        conn, row["atom_id"],
        {"claim": current["claim"], "mechanism": current["mechanism"],
         "conditions": current["conditions"], "counter": current["counter"]},
        {"claim": row["old_claim"], "mechanism": row["old_mechanism"],
         "conditions": row["old_conditions"], "counter": row["old_counter"]},
        "manual-revert", "reverted", f"revert #{revision_id}",
    )
    return {"atom_id": row["atom_id"], "reverted_from": revision_id,
            "claim": row["old_claim"]}
