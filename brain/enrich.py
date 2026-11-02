"""LLM 富化：把「提交摘要」升级为 claim + mechanism + counter。

铁律：
- 锚点一律不动，只在原 atom 上补文字；
- 证据不足时允许模型 skip，不逼它编；
- 机器验证（长度、泛化话术、路径幻觉）不过就拒收，并记录到 revisions；
- 拒收率是抽取器质量指标，不是要消灭的敌人。
"""
from __future__ import annotations

import re
import time

from . import db, gitutil, llm, redact

PROMPT_VERSION = "enrich-v1"

GENERIC = ["需要注意", "值得注意", "可能存在", "优化空间", "建议使用", "尽量", "可以参考"]
# 早期富化版本的占位话术：字段非空但零信息量，重跑富化时按"缺失"处理
PLACEHOLDER_CONDITIONS = ("该缺陷可被测试复现", "可通过测试复现", "可被测试复现")
PATH_RE = re.compile(
    r"(?:[A-Za-z0-9_.\-]+/)*[A-Za-z0-9_.\-]+\.(?:py|java|ts|tsx|js|go|sql|sh|md|json|ya?ml)"
)

SYSTEM = (
    "你是资深后端工程师，负责把真实缺陷修复提炼成可复用经验。"
    "只依据给定证据，不推测未出现的代码、路径或数字。"
    "输出必须是 JSON。"
)

TEMPLATE = """下面是一次真实的缺陷修复提交。请提炼一条可复用经验。

要求：
1. claim：一句可证伪的陈述，格式为「在X条件下做Y会导致Z」。不要复述提交标题，不要写「修复了某问题」这类过程描述。
2. mechanism：因果解释（因为A，所以B）。不要写口号，不要写「需要注意」。
3. conditions：这条经验在什么场景下会被触发/适用（写具体的代码形态或调用路径，不要写「该缺陷可被测试复现」）。
4. counter：这条经验在什么情况下不成立（反例或边界）。
5. 只使用给定 diff 与提交信息。证据不足就返回 {{"skip": true, "reason": "证据不足的具体原因"}}。
6. 不要编造未在证据中出现的文件路径、函数名、常量或数字。

提交标题：{subject}
提交正文：{body}
改动文件：{files}

diff（截断）：
{diff}

只输出 JSON，二选一：
{{"claim": "...", "mechanism": "...", "conditions": "...", "counter": "..."}}
{{"skip": true, "reason": "..."}}
"""


def select(conn, limit: int | None = None, only_id: str | None = None,
           refill: bool = False) -> list:
    if only_id:
        rows = list(conn.execute(
            "SELECT * FROM atoms WHERE id=? AND status!='retired'", (only_id,)))
        return rows
    if refill:
        # 只挑 conditions 被写成占位话术的已富化条目：它们有机制也有反例，缺的只是适用条件
        marks = " OR ".join("conditions LIKE ?" for _ in PLACEHOLDER_CONDITIONS)
        sql = ("SELECT * FROM atoms WHERE status!='retired' AND derivation='llm-enriched'"
               f" AND (conditions='' OR {marks}) ORDER BY created_at DESC")
        args = [f"%{p}%" for p in PLACEHOLDER_CONDITIONS]
        if limit:
            sql += f" LIMIT {int(limit)}"
        return list(conn.execute(sql, args))
    sql = ("SELECT * FROM atoms WHERE status!='retired' AND derivation='git-signal'"
           " AND claim LIKE '缺陷修复同时补充了测试：%' ORDER BY created_at DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql))


def _commit_context(repo: str, sha: str, max_diff: int = 4000) -> tuple[str, str, str]:
    """返回 (提交标题, 提交正文, diff)。

    标题必须从提交里取而不是从 claim 里切：二次富化时 claim 已经是经验陈述，
    再拿它当"提交标题"喂给模型会把模型带偏。
    """
    code, raw = gitutil.git(repo, "show", "-s", "--format=%B", sha)
    lines = (raw if code == 0 else "").strip().splitlines()
    subject = lines[0].strip() if lines else ""
    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
    code, diff = gitutil.git(repo, "show", "--unified=1", "--format=", sha)
    diff = diff if code == 0 else ""
    if len(diff) > max_diff:
        diff = diff[:max_diff] + "\n...（已截断）"
    return subject, body, diff


def validate(payload: dict, old_claim: str, allowed_paths: list[str],
             require_change: bool = True) -> tuple[bool, str]:
    if payload.get("skip"):
        return False, f"skip: {str(payload.get('reason', ''))[:80]}"
    claim = str(payload.get("claim", "")).strip()
    mechanism = str(payload.get("mechanism", "")).strip()
    counter = str(payload.get("counter", "")).strip()
    if len(claim) < 15:
        return False, "claim 过短"
    if len(claim) > 220:
        return False, "claim 过长"
    if len(mechanism) < 12:
        return False, "mechanism 过短"
    if len(counter) < 5:
        return False, "counter 缺失"
    if require_change and claim == old_claim:
        return False, "claim 未发生变化（仍是提交摘要）"
    for word in GENERIC:
        if word in claim:
            return False, f"含泛化话术「{word}」"
    allowed = " ".join(allowed_paths)
    for token in PATH_RE.findall(claim + " " + mechanism):
        base = token.rsplit("/", 1)[-1]
        if token not in allowed and base not in allowed:
            return False, f"疑似路径幻觉：{token}"
    return True, "ok"


def run(conn, limit: int | None = None, model: str = llm.DEFAULT_MODEL,
        dry_run: bool = False, sleep_s: float = 0.4, only_id: str | None = None,
        client=None, progress=None, refill: bool = False) -> dict:
    rows = select(conn, limit=limit, only_id=only_id, refill=refill)
    stats = {"selected": len(rows), "accepted": 0, "rejected": 0,
             "skipped": 0, "errors": 0, "dry_run": dry_run, "details": []}
    for row in rows:
        anchors = db.anchors_of(conn, row["id"])
        files = [a["path"] for a in anchors if a["path"]]
        subject, body, diff = _commit_context(row["repo"], row["commit_sha"])
        if not subject and not diff:
            # 仓库不可读（已删除或非 git）时没有任何证据，硬编只会产出幻觉
            stats["rejected"] += 1
            stats["skipped"] += 1
            item = {"id": row["id"], "verdict": "rejected", "reason": "无提交证据（仓库不可读）"}
            stats["details"].append(item)
            if progress:
                progress(item)
            continue
        if not subject:
            # 拿不到提交（仓库已删/非 git）才退回从 claim 里切
            subject = row["claim"].split("：", 1)[-1]
        prompt = TEMPLATE.format(subject=subject, body=body[:500],
                                 files=", ".join(files[:8]) or "(未知)", diff=diff)
        try:
            payload = (client or llm.chat_json)(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": prompt}], model=model)
        except Exception as exc:  # 单条失败不阻断批量
            stats["errors"] += 1
            item = {"id": row["id"], "verdict": "error",
                    "reason": f"{type(exc).__name__}: {exc}"[:120]}
            stats["details"].append(item)
            if progress:
                progress(item)
            continue
        payload, _ = redact.clean_fields(payload)
        ok, reason = validate(payload, row["claim"], files,
                              require_change=row["derivation"] != "llm-enriched")
        if not ok:
            stats["rejected"] += 1
            if reason.startswith("skip"):
                stats["skipped"] += 1
            if not dry_run:
                db.save_revision(conn, row["id"], dict(row), payload, model, "rejected", reason)
            stats["details"].append({"id": row["id"], "verdict": "rejected", "reason": reason})
        else:
            stats["accepted"] += 1
            if not dry_run:
                db.save_revision(conn, row["id"], dict(row), payload, model, "accepted")
                db.apply_enrichment(conn, row["id"], payload)
            stats["details"].append({"id": row["id"], "verdict": "accepted",
                                     "claim": payload["claim"][:80]})
        if progress:
            progress(stats["details"][-1])
        if sleep_s:
            time.sleep(sleep_s)
    stats["accept_rate"] = round(stats["accepted"] / len(rows), 3) if rows else 0.0
    return stats
