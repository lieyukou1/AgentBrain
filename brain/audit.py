"""语义审查：拿已有经验逐条判断新代码是否重犯。

与 probes 的分工：
- probes 是确定性规则，只认属性缺失，零误报但也只能覆盖已写成规则的坑；
- audit 是语义判断，能发现规则没覆盖的同类问题，代价是可能说错。

所以每条结论都必须自证：verdict=violates 时必须从 diff 新增行里逐字抄一行，
抄不出来就当场作废。这是这套东西唯一可信的地方——错的判断不能留在结果里。
"""
from __future__ import annotations

import json

from . import db, llm, probes

CANDIDATE_LIMIT = 4
MIN_EVIDENCE = 8
VERDICTS = ("violates", "not_applicable", "unclear")

SYSTEM = (
    "你是资深后端与智能体系统代码审查者。只依据给定的 diff 新增行做判断，"
    "不推测 diff 之外的代码、文件或数字。输出必须是 JSON。"
)

TEMPLATE = """下面是若干条历史经验，和一段代码改动（unified diff 的新增行）。

请逐条判断：这次改动是否重犯了该经验描述的缺陷？

经验：
{atoms}

新增行（行号: 内容）：
{lines}

要求：
1. 每条经验给出 verdict：violates（确实重犯）/ not_applicable（这段改动不涉及）/ unclear（证据不足）。
2. verdict=violates 时必须给 evidence：从上面的新增行里原样抄**一整行**（不含行号前缀）。
3. 不要编造行号、文件名或未出现在上面的代码。
4. reason 用一句话说明为什么会重犯，不要复述经验原文。

只输出 JSON：
{{"results": [{{"atom_id": "a-xxxxxxxxxxxx", "verdict": "violates|not_applicable|unclear",
  "evidence": "原样抄的一行", "reason": "一句话"}}]}}
"""


def run(conn, diff_text: str, atom_ids: list[str] | None = None,
        model: str = llm.DEFAULT_MODEL, client=None, record: bool = True) -> dict:
    files = probes.parse_diff(diff_text, code_only=True)
    added = [(no, line) for lines in files.values() for no, line in lines]
    atoms = _candidates(conn, files, atom_ids)

    if not atoms:
        return {"files_scanned": len(files), "candidates": 0, "results": [],
                "rejected_evidence": 0, "recorded": 0,
                "note": "diff 涉及的文件上没有已沉淀经验，语义审查没有输入"}
    if not added:
        return {"files_scanned": len(files), "candidates": len(atoms), "results": [],
                "rejected_evidence": 0, "recorded": 0, "note": "diff 没有新增代码行"}

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": TEMPLATE.format(
            atoms=_format_atoms(atoms),
            lines="\n".join(f"{no}: {line}" for no, line in added[:200]),
        )},
    ]
    payload = (client or llm.chat_json)(messages, model=model)
    results, rejected = verify(payload, atoms, added)
    recorded = record_violations(conn, results) if record else 0
    return {"files_scanned": len(files), "candidates": len(atoms),
            "results": results, "rejected_evidence": rejected, "recorded": recorded}


def record_violations(conn, results: list[dict]) -> int:
    """把机检通过的 violates 记成一次「疑似命中」，只留事件、不动置信度。

    这一步是为了不让使用反馈断在「报了但没人记」上；
    但 LLM 的判断不能当成已验证，所以 adjust=False：
    加置信度、改状态仍然要等真实任务里的人工确认。
    """
    count = 0
    for item in results:
        if item["verdict"] != "violates":
            continue
        evidence = str(item.get("evidence", ""))[:80]
        db.record_outcome(conn, item["atom_id"], "validated",
                          f"audit: {evidence}".strip(), adjust=False)
        count += 1
    return count


def _candidates(conn, files: dict, atom_ids: list[str] | None) -> list:
    if atom_ids:
        rows = [db.get_atom(conn, atom_id) for atom_id in atom_ids]
        return [row for row in rows if row is not None and row["status"] != "retired"]
    return probes.related_atoms(conn, files, limit=CANDIDATE_LIMIT)


def _format_atoms(atoms: list) -> str:
    parts = []
    for row in atoms:
        parts.append(
            f"[{row['id']}] {row['claim']}\n"
            f"  机制：{row['mechanism'] or '（无）'}\n"
            f"  适用：{row['conditions'] or '（无）'}\n"
            f"  不成立：{row['counter'] or '（无）'}"
        )
    return "\n\n".join(parts)


def verify(payload: dict, atoms: list, added: list[tuple[int, str]]) -> tuple[list, int]:
    """机检模型结论：atom 必须来自候选集，violates 必须抄得出真实新增行。"""
    known = {row["id"]: row for row in atoms}
    texts = [" ".join(line.split()) for _, line in added]
    results: list[dict] = []
    rejected = 0
    for item in (payload.get("results") or []):
        if not isinstance(item, dict):
            continue
        atom_id = str(item.get("atom_id", "")).strip()
        verdict = str(item.get("verdict", "")).strip()
        if atom_id not in known or verdict not in VERDICTS:
            continue
        entry = {
            "atom_id": atom_id,
            "verdict": verdict,
            "claim": known[atom_id]["claim"],
            "reason": str(item.get("reason", "")).strip()[:300],
        }
        if verdict == "violates":
            evidence = " ".join(str(item.get("evidence", "")).split())
            if evidence and any(evidence in text for text in texts):
                entry["evidence"] = evidence
            else:
                rejected += 1
                entry["verdict"] = "rejected"
                entry["reason"] = "证据行不在 diff 新增行中（疑似幻觉，已作废）"
        results.append(entry)
    return results, rejected


def dumps(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False)
