"""MCP stdio 服务：JSON-RPC 2.0，逐行消息，无第三方依赖。

可用工具：search / explain / record_atom / check_diff / record_outcome / review_pending / stats
所有工具返回 text content（JSON 字符串），便于任何客户端展示。
initialize 会返回 instructions（何时用哪个工具），工具描述也写成"何时用"口径，
因为客户端往往只把工具名与描述露给模型，不读这段就等于没有入口。
每次 tools/call 追加一行到 logs/mcp-calls.jsonl，用于回答「到底被调了几次」。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

from . import audit, calllog, db, funnel, ingest, probes, search

SERVER_INFO = {"name": "agentbrain", "version": "0.1.0"}
PROTOCOL = "2024-11-05"

INSTRUCTIONS = """AgentBrain 是本地经验库：只存「带证据锚点、可被证伪」的经验，不是文档全文检索。

何时用：
- 动手改一块不熟的代码前：search(2-4 个关键词)，先看有没有踩过的坑。
- 写完改动、提交前：把本次 unified diff 交给 check_diff，查并发/幂等/超时/缓存/后台任务等遗漏。
- check_diff 没报但心里没底：audit_diff 拿同文件的历史经验做语义对照，结论必须附 diff 原行。
- audit_diff 确认某条确实命中：再用 record_outcome 记一次正式反馈（它自己只记疑似命中，不加分）。
- 本次踩到新坑并已定位：record_atom 落库（必须带 commit 或 path 锚点）。
- 审完候选要落地：review_atom（good 采纳 / bad 退休）；只想看列表用 review_pending。
- 某条经验帮到或没帮到：record_outcome 记一次，驱动其置信度。

不要用：
- 问「这个函数做什么」「这段代码怎么改」——直接读代码或 Grep 更快更准。
- 不要指望同义检索：search 是关键词 AND 匹配，零命中就是零命中，不要用其它结果凑数。

返回为空只代表库内没有相关经验，不代表没有风险。"""

TOOLS = [
    {
        "name": "search",
        "description": (
            "何时用：动手修改不熟悉的代码前，或不确定某类问题是否踩过坑时，用 2-4 个关键词"
            "检索已沉淀经验。返回 claim、机制、触发条件、反例与证据锚点（commit/文件/片段）。"
            "关键词是 AND 匹配，零命中即库内没有，不要据此认为无风险。"
            "查「这段代码在做什么」不要用它，直接读代码。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "问题或代码上下文关键词"},
                "limit": {"type": "integer", "default": 8},
                "repo": {"type": "string",
                         "description": "可选：只在该仓库内检索，库里收录了多个项目时用它收窄"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "explain",
        "description": (
            "何时用：search 或 check_diff 返回了某条经验的 id，采纳前要核实它的完整机制、"
            "全部证据锚点与历史使用反馈时调用。不要用它做批量浏览，那用 review_pending。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"atom_id": {"type": "string"}},
            "required": ["atom_id"],
        },
    },
    {
        "name": "record_atom",
        "description": (
            "何时用：本会话确实踩到一个坑、并已定位到具体代码位置时，把它写成一条可证伪经验入库。"
            "claim 写「在 X 条件下做 Y 会导致 Z」，并给出 mechanism（因果）与 counter（何时不成立）。"
            "必须带 commit 或 path 锚点，写入后会立即机检锚点能否解析。"
            "仅凭猜测、未复现或说不清机制的问题不要写入。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "claim": {"type": "string", "description": "一句可证伪的陈述：在什么条件下做什么会导致什么"},
                "mechanism": {"type": "string", "description": "为什么（因果，不是口号）"},
                "conditions": {"type": "string", "description": "触发条件"},
                "counter": {"type": "string", "description": "反例/什么情况下不成立"},
                "repo": {"type": "string", "description": "仓库绝对路径；省略则用第一个登记的源"},
                "commit": {"type": "string", "description": "相关提交 sha"},
                "path": {"type": "string", "description": "仓库内相对路径"},
                "line_start": {"type": "integer"},
                "line_end": {"type": "integer"},
                "excerpt": {"type": "string", "description": "锚定行的原文片段，用于机检"},
            },
            "required": ["claim"],
        },
    },
    {
        "name": "check_diff",
        "description": (
            "何时用：写完一段改动、提交或交付前，把 git diff 的 unified 原文传进来，"
            "检查是否遗漏超时、幂等、并发同步、缓存失效、后台任务生命周期等防护，"
            "并拿到与之相关的既有经验。只传片段会漏判，尽量给完整 diff。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"diff_text": {"type": "string", "description": "unified diff 原文"}},
            "required": ["diff_text"],
        },
    },
    {
        "name": "record_outcome",
        "description": (
            "何时用：某条经验在本次任务中确实帮你避开了问题（validated），或看了但没用上"
            "（insufficient）时调用，它会调整该条置信度，长期把没用的条目压下去。"
            "不要凭感觉批量刷，只记真实发生过的判定。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "atom_id": {"type": "string"},
                "outcome": {"type": "string", "enum": ["validated", "insufficient"]},
                "note": {"type": "string"},
            },
            "required": ["atom_id", "outcome"],
        },
    },
    {
        "name": "audit_diff",
        "description": (
            "何时用：check_diff 报不出问题、但你想知道「这段改动有没有重犯已知的坑」时调用。"
            "它拿库里同文件的历史经验逐条做语义判断，能覆盖规则探针抓不到的同类缺陷。"
            "每条 violates 结论必须附 diff 里的原行，抄不出来会当场作废；"
            "没有候选经验时返回空并说明，不要据此认为代码没问题。比 check_diff 贵，别每次改动都跑。"
            "报出的每条 violates 会自动记一次「疑似命中」到该经验的使用反馈里（不改置信度），"
            "你确认确实命中后，再用 record_outcome 给它正式记一次。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "diff_text": {"type": "string", "description": "unified diff 原文"},
                "atom_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "指定要对照的经验 id；省略则自动取 diff 涉及的代码文件上的经验",
                },
                "record": {
                    "type": "boolean",
                    "default": True,
                    "description": "是否把 violates 记成一次疑似命中；只想看结论不记账时设为 false",
                },
            },
            "required": ["diff_text"],
        },
    },
    {
        "name": "review_atom",
        "description": (
            "何时用：review_pending 或 search 给出了候选 id，你已经判断它成立（good 采纳）"
            "或不成立（bad 退休）时调用。good 要求至少一条锚点已解析，否则先跑 brain verify。"
            "这一步会写入状态并记一次使用反馈；只想知道有哪些候选、不做决定，用 review_pending。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "atom_id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["good", "bad"]},
                "note": {"type": "string", "description": "判断依据，会写进使用反馈"},
            },
            "required": ["atom_id", "verdict"],
        },
    },
    {
        "name": "review_pending",
        "description": (
            "何时用：需要人工过一遍候选经验、决定采纳还是退休时调用，按置信度列出待审条目。"
            "只读，不会改变任何状态；决定要落地请调用 review_atom。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    },
    {
        "name": "stats",
        "description": (
            "何时用：想知道库里有多少条、锚点解决率、边数与使用反馈数，或确认某个仓库是否已被"
            "收录（sources）时调用。属于健康度诊断，不要当作检索用。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _dispatch(conn, name: str, args: dict) -> dict:
    if name == "search":
        # fallback 必须关掉：无命中就该返回空，而不是拿最高置信度的条目凑数
        return {"results": search.search_atoms(
            conn, args.get("query", ""), int(args.get("limit", 8)),
            fallback=False, repo=args.get("repo") or None)}
    if name == "explain":
        row = db.get_atom(conn, args.get("atom_id", ""))
        if row is None:
            return {"error": "atom 不存在"}
        return {
            **{k: row[k] for k in row.keys()},
            "anchors": [dict(a) for a in db.anchors_of(conn, row["id"])],
            "edges": [dict(e) for e in conn.execute(
                "SELECT rel,dst,weight FROM edges WHERE src=? ORDER BY weight DESC", (row["id"],))],
        }
    if name == "record_atom":
        return ingest.record(conn, args)
    if name == "check_diff":
        return probes.run_check(conn, args.get("diff_text", ""))
    if name == "audit_diff":
        return audit.run(conn, args.get("diff_text", ""), atom_ids=args.get("atom_ids"),
                         record=bool(args.get("record", True)))
    if name == "record_outcome":
        atom_id = args.get("atom_id", "")
        outcome = args.get("outcome", "")
        if outcome not in ("validated", "insufficient"):
            return {"error": "outcome 必须是 validated 或 insufficient"}
        if db.get_atom(conn, atom_id) is None:
            return {"error": "atom 不存在"}
        db.record_outcome(conn, atom_id, outcome, args.get("note", ""))
        row = db.get_atom(conn, atom_id)
        return {"atom_id": atom_id, "confidence": row["confidence"], "status": row["status"]}
    if name == "review_atom":
        return ingest.review(conn, args.get("atom_id", ""), args.get("verdict", ""),
                             args.get("note", ""))
    if name == "review_pending":
        rows = list(conn.execute(
            "SELECT id,claim,confidence,derivation FROM atoms WHERE status='candidate' "
            "ORDER BY confidence DESC LIMIT ?", (int(args.get("limit", 20)),)))
        return {"pending": [dict(r) for r in rows]}
    if name == "stats":
        # 存量之外再给一层使用情况：只有「库多大」回答不了「到底有没有用」
        return {**db.stats(conn), "usage": funnel.usage()}
    return {"error": f"未知工具: {name}"}


def _arg_brief(args: dict) -> dict:
    """只记参数骨架：长文本（如 diff）换成长度，避免日志被正文撑爆。"""
    brief = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 120:
            brief[k] = f"<{len(v)} chars>"
        elif isinstance(v, (str, int, float, bool)) or v is None:
            brief[k] = v
        else:
            brief[k] = f"<{type(v).__name__}>"
    return brief


def _result_brief(name: str, payload) -> dict:
    """只留可聚合的计数进日志，正文照旧不落盘。"""
    if not isinstance(payload, dict):
        return {}
    if name == "search":
        return {"hits": len(payload.get("results") or [])}
    if name == "check_diff":
        return {"findings": len(payload.get("findings") or []),
                "related": len(payload.get("related_atoms") or [])}
    if name == "audit_diff":
        results = payload.get("results") or []
        return {"candidates": payload.get("candidates", 0),
                "violates": sum(1 for r in results if r.get("verdict") == "violates"),
                "rejected": payload.get("rejected_evidence", 0)}
    if name == "review_pending":
        return {"pending": len(payload.get("pending") or [])}
    if "error" in payload:
        return {"error": True}
    return {}


def _log_call(name: str, args: dict, ok: bool, ms: int, result_chars: int,
              result: dict | None = None) -> None:
    """MCP 调用留痕；写日志失败不能影响工具调用本身。"""
    try:
        calllog.append({
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "tool": name,
            "ok": ok,
            "ms": ms,
            "args": _arg_brief(args),
            "result_chars": result_chars,
            # 命中率只能从结构化的计数算出来，光有字符数看不出"这次到底有没有结果"
            "result": result or {},
        })
    except OSError:
        pass


def handle(conn, msg: dict) -> dict | None:
    if "id" not in msg:
        return None  # notification
    method = msg.get("method", "")
    try:
        if method == "initialize":
            # instructions 是协议内的服务端说明：客户端若展示，模型不必先读描述文件也知道何时用
            result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name", "")
            args = params.get("arguments") or {}
            started = time.perf_counter()
            try:
                payload = _dispatch(conn, name, args)
            except Exception as exc:
                _log_call(name, args, False,
                          int((time.perf_counter() - started) * 1000),
                          len(f"{type(exc).__name__}: {exc}"))
                raise
            text = json.dumps(payload, ensure_ascii=False)
            _log_call(name, args, True, int((time.perf_counter() - started) * 1000), len(text),
                      _result_brief(name, payload))
            result = {"content": [{"type": "text", "text": text}]}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": msg["id"],
                    "error": {"code": -32601, "message": f"Method not found: {method}"}}
    except Exception as exc:  # MCP 层不因单次调用崩溃
        return {"jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


def serve() -> int:
    conn = db.connect()
    out = sys.stdout.buffer
    try:
        for raw in sys.stdin.buffer:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            resp = handle(conn, msg)
            if resp is not None:
                out.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                out.flush()
    finally:
        conn.close()
    return 0
