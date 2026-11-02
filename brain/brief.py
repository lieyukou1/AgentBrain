"""有界快照：会话启动时该看的那一页。

为什么不是"把 summary 全量注入"：经验库会一直长，整份注入的 token 成本
随时间线性上升，而且大部分条目与当前任务无关。这里只给
「高置信结论 + 未验证但值得知道 + 最近变更 + 死路」，并硬性截断。

排序只看置信度与新鲜度，不做语义检索——brief 是索引，不是答案。
"""
from __future__ import annotations

from . import db, search

# 约 1200 个中文 token 的量级；超出的部分一律截断并明确说明
BRIEF_CHARS = 2400
MAX_ACCEPTED = 8
MAX_PENDING = 5
MAX_RECENT = 5
MAX_DEAD_ENDS = 5


def build(conn, repo: str | None = None, budget: int = BRIEF_CHARS) -> dict:
    repo = (repo or "").strip()
    where, params = ["status='accepted'", "superseded_by=''"], []
    if repo:
        where.append("repo=?")
        params.append(repo)
    accepted = list(conn.execute(
        f"SELECT * FROM atoms WHERE {' AND '.join(where)}"
        " ORDER BY confidence DESC, updated_at DESC", params))

    pending = list(conn.execute(
        "SELECT * FROM atoms WHERE status='candidate' AND superseded_by=''"
        + (" AND repo=?" if repo else "") + " ORDER BY updated_at DESC LIMIT ?",
        ([repo] if repo else []) + [MAX_PENDING]))

    recent = list(conn.execute(
        "SELECT * FROM atoms WHERE status='accepted' AND superseded_by=''"
        + (" AND repo=?" if repo else "") + " ORDER BY updated_at DESC LIMIT ?",
        ([repo] if repo else []) + [MAX_RECENT]))

    dead = db.dead_ends_for(conn, repo=repo or None, limit=MAX_DEAD_ENDS)

    lines = [f"# AgentBrain brief{' · ' + repo if repo else ''}",
             f"accepted {len(accepted)}｜candidate {len(pending)}｜dead-ends {len(dead)}", ""]
    lines.append("## 高置信结论")
    if accepted:
        for row in accepted[:MAX_ACCEPTED]:
            lines.append(_atom_line(conn, row))
    else:
        lines.append("- 暂无")
    lines.append("")
    lines.append("## 未验证候选（可能是坑，也可能是噪声）")
    if pending:
        for row in pending:
            lines.append(_atom_line(conn, row))
    else:
        lines.append("- 暂无")
    lines.append("")
    lines.append("## 最近变更")
    if recent:
        for row in recent:
            lines.append(f"- [{row['id']}] {_clip(row['claim'], 80)}（{row['updated_at'][:10]}）")
    else:
        lines.append("- 暂无")
    if dead:
        lines.append("")
        lines.append("## 死路（试过并失败）")
        for item in dead:
            why = _clip(item["why_failed"], 50) or "原因未记录"
            lines.append(f"- [{item['path'] or '未标注'}] {_clip(item['tried'], 60)} → {why}")

    text, truncated = _fit(lines, budget)
    return {
        "text": text,
        "chars": len(text),
        "budget": budget,
        "truncated": truncated,
        "counts": {"accepted": len(accepted), "candidate": len(pending),
                   "dead_ends": len(dead)},
    }


def _atom_line(conn, row) -> str:
    fresh = search.freshness_of(conn, row["id"])
    return (f"- [{row['id']}] {_clip(row['claim'], 90)}"
            f"（conf {row['confidence']:.2f}｜{fresh}）")


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _fit(lines: list[str], budget: int) -> tuple[str, bool]:
    """逐行累加，超预算就停；被截断时明确写出来，避免读的人以为这就是全部。"""
    out, used = [], 0
    for line in lines:
        if used + len(line) + 1 > budget:
            out.append("…（已达 brief 上限，更多内容用 search 查）")
            return "\n".join(out), True
        out.append(line)
        used += len(line) + 1
    return "\n".join(out), False
