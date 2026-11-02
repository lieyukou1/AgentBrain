"""检索回归：用真实查询 + 人工标注的期望条目衡量召回，而不是看"返回了几条"。

标注口径：expect 只收能直接回答这个问题的经验，相邻话题不算。
expect 为空表示"库内确实没有"，此时返回任何条目都算一次误报——这是唯一能测噪声的样本。

为什么要它：优化检索时，"感觉变好了"不可验证。没有固定查询集，
改权重、改门槛、改分层都只是在换一个主观感受。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import search

EVAL_PATH = Path(__file__).resolve().parents[1] / "eval" / "queries.json"
# 一次检索返回超过这个字节数，说明"只给接近最佳命中的几条"又退化了
TOKEN_BUDGET_BYTES = 8 * 1024


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * q))], 2)


def load(path: str | Path | None = None) -> dict:
    target = Path(path) if path else EVAL_PATH
    return json.loads(target.read_text(encoding="utf-8-sig"))


def run(conn, path: str | Path | None = None, limit: int = 5) -> dict:
    data = load(path)
    rows = []
    for case in data["queries"]:
        query = case["query"]
        expect = list(case.get("expect") or [])
        repo = case.get("repo")
        started = time.perf_counter()
        hits = search.search_atoms(conn, query, limit=limit, fallback=False, repo=repo,
                                   brief=True)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        got = [h["id"] for h in hits]
        target = search._norm_repo(repo) if repo else ""
        leaked = sum(1 for h in hits
                     if target and search._norm_repo(h.get("repo")) != target)
        stale = sum(1 for h in hits if h.get("freshness") == "missing")
        payload_bytes = len(json.dumps(hits, ensure_ascii=False).encode("utf-8"))
        rows.append({
            "query": query,
            "expected": expect,
            "got": got,
            "top1_ok": bool(expect) and bool(got) and got[0] in expect,
            "hit_at_3": bool(expect) and any(i in expect for i in got[:3]),
            "false_positive": not expect and bool(got),
            "top_score": hits[0]["score"] if hits else 0,
            "leaked": leaked,
            "stale_shown": stale,
            "bytes": payload_bytes,
            "latency_ms": elapsed_ms,
        })
    graded = [r for r in rows if r["expected"]]
    negatives = [r for r in rows if not r["expected"]]

    def ratio(n: int, d: int) -> float | None:
        return round(n / d, 3) if d else None

    # 精确率只看前 3 条：召回要求看全，但噪声是按用户真正会读的条数算的
    precision_hits = sum(1 for r in graded for i in r["got"][:3] if i in r["expected"])
    precision_total = sum(len(r["got"][:3]) for r in graded)
    latency = [r["latency_ms"] for r in rows]
    scoped_rows = [r for r in rows if _case_repo(r["query"], data)]
    return {
        "queries": len(rows),
        "graded": len(graded),
        "recall_at_1": ratio(sum(r["top1_ok"] for r in graded), len(graded)),
        "recall_at_3": ratio(sum(r["hit_at_3"] for r in graded), len(graded)),
        "precision_at_3": ratio(precision_hits, precision_total),
        "zero_hit": sum(1 for r in graded if not r["got"]),
        "negatives": len(negatives),
        "false_positives": sum(r["false_positive"] for r in negatives),
        "abstention_accuracy": ratio(
            sum(1 for r in negatives if not r["false_positive"]), len(negatives)),
        "scope_isolation": ratio(
            sum(1 for r in scoped_rows if not r["leaked"]), len(scoped_rows)),
        "stale_shown": sum(r["stale_shown"] for r in rows),
        "stale_rejection": 1.0 if all(r["stale_shown"] == 0 for r in rows) else 0.0,
        "token_budget_bytes": TOKEN_BUDGET_BYTES,
        "over_budget": sum(1 for r in rows if r["bytes"] > TOKEN_BUDGET_BYTES),
        "latency_p50_ms": _percentile(latency, 0.5),
        "latency_p95_ms": _percentile(latency, 0.95),
        "rows": rows,
    }


def _case_repo(query: str, data: dict) -> str:
    for case in data["queries"]:
        if case["query"] == query:
            return case.get("repo") or ""
    return ""
