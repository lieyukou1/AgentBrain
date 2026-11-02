"""使用漏斗：从「存了多少」走到「有没有起作用」。

四层，逐层收窄：
  知识层  库里有多少条、多少可依赖
  加工层  富化到底接受率如何
  使用层  近 N 天被调用多少次、命中率如何
  反馈层  有多少经验真的在任务里被验证过

刻意区分「自动采纳」与「真实使用反馈」：triage 只证明格式合格，
只有来自任务里的 validated/invalidated 才证明这条经验有用。
这个差别不标出来，181 条 accepted 会看着像 181 条已验证知识。
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

from . import calllog


def _read_calls() -> list[dict]:
    return calllog.read()


def _one(conn, sql: str, *args):
    row = conn.execute(sql, args).fetchone()
    return row[0] if row else 0


def knowledge(conn) -> dict:
    by_status = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM atoms GROUP BY status")}
    anchors = _one(conn, "SELECT COUNT(*) FROM anchors")
    resolved = _one(conn, "SELECT COUNT(*) FROM anchors WHERE resolved=1")
    return {
        "atoms": _one(conn, "SELECT COUNT(*) FROM atoms"),
        "by_status": by_status,
        "anchors": anchors,
        "anchor_resolution_rate": round(resolved / anchors, 3) if anchors else 0.0,
        "edges": _one(conn, "SELECT COUNT(*) FROM edges"),
        "pending_clues": _one(conn, "SELECT COUNT(*) FROM clues WHERE status='pending'"),
    }


def processing(conn) -> dict:
    accepted = _one(conn, "SELECT COUNT(*) FROM revisions WHERE verdict='accepted'")
    rejected = _one(conn, "SELECT COUNT(*) FROM revisions WHERE verdict='rejected'")
    total = accepted + rejected
    by_derivation = {r["derivation"]: r["n"] for r in conn.execute(
        "SELECT derivation, COUNT(*) n FROM atoms GROUP BY derivation")}
    return {
        "enrich_accepted": accepted,
        "enrich_rejected": rejected,
        "enrich_accept_rate": round(accepted / total, 3) if total else 0.0,
        "by_derivation": by_derivation,
    }


def usage(days: int = 7, today: date | None = None) -> dict:
    today = today or date.today()
    cutoff = (today - timedelta(days=days - 1)).isoformat()
    calls = [c for c in _read_calls() if str(c.get("ts", ""))[:10] >= cutoff]
    by_tool = Counter(str(c.get("tool")) for c in calls)
    ok = sum(1 for c in calls if c.get("ok"))
    durations = sorted(int(c.get("ms") or 0) for c in calls)

    def outcome_of(tool: str, key: str) -> tuple[int, int]:
        """返回 (可判定次数, 命中次数)。旧日志没有 result 摘要，只能跳过。"""
        judged = [c for c in calls
                  if c.get("tool") == tool and isinstance(c.get("result"), dict)
                  and key in c["result"]]
        hit = sum(1 for c in judged if c["result"][key])
        return len(judged), hit

    searched, search_hit = outcome_of("search", "hits")
    checked, check_hit = outcome_of("check_diff", "findings")
    # 只有这两类调用的结果才谈得上"命中"；stats/record_atom 不参与命中率
    judgeable = sum(1 for c in calls if c.get("tool") in ("search", "check_diff"))

    # 命中强度与分层：判断"检索变差"还是"库变杂"只能看 top_score 的时间序列，
    # 光有命中率看不出返回的是不是勉强及格的条目。
    graded = [c for c in calls if c.get("tool") == "search"
              and isinstance(c.get("result"), dict) and "top_score" in c["result"]]
    top_scores = sorted(float(c["result"].get("top_score") or 0) for c in graded)
    full = sum(1 for c in graded
               if int(c["result"].get("asked") or 0) > 0
               and int(c["result"].get("hits") or 0) >= int(c["result"]["asked"]))
    tiers: Counter = Counter()
    for c in graded:
        for name, n in (c["result"].get("tiers") or {}).items():
            tiers[str(name)] += int(n)

    def pick(values: list[float], q: float) -> float | None:
        if not values:
            return None
        return round(values[min(len(values) - 1, int(len(values) * q))], 2)

    return {
        "window_days": days,
        "calls": len(calls),
        "ok_rate": round(ok / len(calls), 3) if calls else 0.0,
        "ms_p50": durations[len(durations) // 2] if durations else 0,
        "by_tool": dict(by_tool),
        "search_calls": searched,
        "search_hit_rate": round(search_hit / searched, 3) if searched else None,
        "search_graded": len(graded),
        "search_top_score_p50": pick(top_scores, 0.5),
        "search_top_score_p90": pick(top_scores, 0.9),
        "search_full_rate": round(full / len(graded), 3) if graded else None,
        "search_tiers": dict(tiers),
        "check_calls": checked,
        "check_hit_rate": round(check_hit / checked, 3) if checked else None,
        "unjudged": judgeable - searched - checked,
    }


def feedback(conn) -> dict:
    total = _one(conn, "SELECT COUNT(*) FROM outcomes")
    validated = _one(conn, "SELECT COUNT(*) FROM outcomes WHERE outcome='validated'")
    auto = _one(conn, "SELECT COUNT(*) FROM outcomes WHERE note LIKE 'triage:%'"
                     " OR note LIKE 'auto-pruned%'")
    # audit 是 LLM 报出的疑似命中，同样只能算线索，不能算"真实使用验证"
    audit_events = _one(conn, "SELECT COUNT(*) FROM outcomes WHERE note LIKE 'audit:%'")
    audit_atoms = _one(conn, "SELECT COUNT(DISTINCT atom_id) FROM outcomes"
                            " WHERE note LIKE 'audit:%'")
    real = _one(conn, "SELECT COUNT(DISTINCT atom_id) FROM outcomes"
                      " WHERE note NOT LIKE 'triage:%' AND note NOT LIKE 'auto-pruned%'"
                      " AND note NOT LIKE 'audit:%'")
    accepted = _one(conn, "SELECT COUNT(*) FROM atoms WHERE status='accepted'")
    return {
        "outcomes": total,
        "validated": validated,
        "validated_rate": round(validated / total, 3) if total else 0.0,
        "auto_graded": auto,
        "audit_hits": audit_events,
        "audit_atoms": audit_atoms,
        "real_feedback_atoms": real,
        # 这是整个漏斗的落点：多少条"可依赖"其实只是"格式合格"
        "accepted_without_real_feedback": max(0, accepted - real),
    }


def report(conn, days: int = 7) -> dict:
    return {
        "knowledge": knowledge(conn),
        "processing": processing(conn),
        "usage": usage(days),
        "feedback": feedback(conn),
    }
