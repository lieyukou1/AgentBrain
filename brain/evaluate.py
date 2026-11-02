"""检索回归：用真实查询 + 人工标注的期望条目衡量召回，而不是看"返回了几条"。

标注口径：expect 只收能直接回答这个问题的经验，相邻话题不算。
expect 为空表示"库内确实没有"，此时返回任何条目都算一次误报——这是唯一能测噪声的样本。

为什么要它：优化检索时，"感觉变好了"不可验证。没有固定查询集，
改权重、改门槛、改分层都只是在换一个主观感受。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import search

EVAL_PATH = Path(__file__).resolve().parents[1] / "eval" / "queries.json"


def load(path: str | Path | None = None) -> dict:
    target = Path(path) if path else EVAL_PATH
    return json.loads(target.read_text(encoding="utf-8-sig"))


def run(conn, path: str | Path | None = None, limit: int = 5) -> dict:
    data = load(path)
    rows = []
    for case in data["queries"]:
        query = case["query"]
        expect = list(case.get("expect") or [])
        hits = search.search_atoms(conn, query, limit=limit, fallback=False,
                                   repo=case.get("repo"))
        got = [h["id"] for h in hits]
        rows.append({
            "query": query,
            "expected": expect,
            "got": got,
            "top1_ok": bool(expect) and bool(got) and got[0] in expect,
            "hit_at_3": bool(expect) and any(i in expect for i in got[:3]),
            "false_positive": not expect and bool(got),
            "top_score": hits[0]["score"] if hits else 0,
        })
    graded = [r for r in rows if r["expected"]]
    negatives = [r for r in rows if not r["expected"]]

    def ratio(n: int, d: int) -> float | None:
        return round(n / d, 3) if d else None

    return {
        "queries": len(rows),
        "graded": len(graded),
        "recall_at_1": ratio(sum(r["top1_ok"] for r in graded), len(graded)),
        "recall_at_3": ratio(sum(r["hit_at_3"] for r in graded), len(graded)),
        "zero_hit": sum(1 for r in graded if not r["got"]),
        "negatives": len(negatives),
        "false_positives": sum(r["false_positive"] for r in negatives),
        "rows": rows,
    }
