"""MCP stdio 服务：JSON-RPC 2.0，逐行消息，无第三方依赖。

可用工具：search / explain / record_atom / check_diff / audit_diff / record_outcome /
revoke_outcome / review_atom / review_pending / stats。
所有工具返回 text content（JSON 字符串），便于任何客户端展示。
initialize 会返回 instructions（何时用哪个工具），工具描述也写成"何时用"口径，
因为客户端往往只把工具名与描述露给模型，不读这段就等于没有入口。
每次 tools/call 追加一行到 logs/mcp-calls.jsonl，用于回答「到底被调了几次」。
"""
from __future__ import annotations

import contextlib
import io
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
            "默认只给 3 条、只给接近最佳命中的结果，零命中就是库内没有，不要用其它结果凑数，"
            "也不要据此认为无风险。若同一主题下有「试过并失败」的死路，会放在 dead_ends 里一并返回。"
            "查「这段代码在做什么」不要用它，直接读代码。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "问题或代码上下文关键词"},
                "limit": {"type": "integer", "default": 3},
                "repo": {"type": "string",
                         "description": "可选：只在该仓库内检索，库里收录了多个项目时用它收窄"},
                "freshness": {"type": "string", "enum": ["ok", "partial", "unverified"],
                              "description": "可选：只看指定证据状态的条目"},
                "stack": {"type": "string",
                          "description": "可选：当前项目技术栈（python/node/java/go/rust/docker），"
                                         "跨仓库检索时同栈条目优先；省略时按 repo/服务进程 cwd 自动识别"}
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
                "supersedes": {"type": "string",
                               "description": "可选：被这条新结论取代的旧 atom id；旧条会退休但保留历史"},
            },
            "required": ["claim"],
        },
    },
    {
        "name": "record_dead_end",
        "description": (
            "何时用：一次修复尝试失败或只部分奏效时，把「试过什么、为什么不行、最后什么有效」"
            "记下来。它不进 accepted 统计，只用于下次改同一文件前提醒不要再走这条路。"
            "必须给 path；why_failed 与 what_worked 尽量写具体，否则下个会话无法判断。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tried": {"type": "string", "description": "试过的做法"},
                "why_failed": {"type": "string", "description": "为什么没成"},
                "what_worked": {"type": "string", "description": "最后有效的做法"},
                "conditions": {"type": "string", "description": "在什么条件下"},
                "path": {"type": "string", "description": "仓库内相对路径，必填"},
                "commit": {"type": "string"},
                "repo": {"type": "string", "description": "仓库绝对路径；省略则用第一个登记的源"},
            },
            "required": ["tried", "path"],
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
            "何时用：某条经验在本次任务中起了作用、或验证失败、或被证明是错的时调用。"
            "validated=确实避免了问题；used=只提供了线索；not-used=看了没用上；"
            "failed=按它做但失败了；corrected=结论本身被纠正。它会调整置信度，"
            "长期把没用的条目压下去。不要凭感觉批量刷，只记真实发生过的判定。"
            "探针误报用 probe_false_positive，note 写成 probe:<探针 id>，"
            "误报率过高时 check_diff 会自动把该探针降级为线索。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "atom_id": {"type": "string"},
                "outcome": {"type": "string", "enum": sorted(db.FEEDBACK_DELTAS)},
                "note": {"type": "string"},
            },
            "required": ["atom_id", "outcome"],
        },
    },
    {
        "name": "revoke_outcome",
        "description": (
            "何时用：发现某次 validated / insufficient 记错了（把 A 的验证记到了 B 上、"
            "或当时判断有误）时撤销它。事件保留为 revoked，置信度反向回滚；"
            "不要用它修改结论本身——结论变了应该记一条新经验并 supersede。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "outcome_id": {"type": "integer", "description": "反馈事件 id（explain 可见）"},
            },
            "required": ["outcome_id"],
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
        query = args.get("query", "")
        results = search.search_atoms(
            conn, query, int(args.get("limit", 3)),
            fallback=False, repo=args.get("repo") or None, brief=True,
            freshness=args.get("freshness") or None, stack=args.get("stack") or None)
        dead = search.dead_ends(conn, query)
        payload = {
            "results": results,
            "dead_ends": dead,
            "next": "需要机制、反例与证据锚点时，用 explain <id> 取完整条目",
        }
        if not results and not dead:
            # 空结果必须说清楚是"库里没有"，否则模型容易当成"没有风险"
            payload["guidance"] = ("库内没有相关经验。若这次确实踩到可复现的坑，"
                                   "用 record_atom 写入（需 commit 或 path 锚点）；"
                                   "失败的尝试用 record_dead_end 记录。")
        return payload
    if name == "explain":
        row = db.get_atom(conn, args.get("atom_id", ""))
        if row is None:
            return {"error": "atom 不存在"}
        return {
            **{k: row[k] for k in row.keys()},
            "freshness": search.freshness_of(conn, row["id"]),
            "anchors": [dict(a) for a in db.anchors_of(conn, row["id"])],
            "edges": [dict(e) for e in conn.execute(
                "SELECT rel,dst,weight FROM edges WHERE src=? ORDER BY weight DESC", (row["id"],))],
            "outcomes": [dict(o) for o in conn.execute(
                "SELECT id,outcome,note,revoked,created_at FROM outcomes WHERE atom_id=? ORDER BY id",
                (row["id"],))],
            "supersedes": [dict(r) for r in conn.execute(
                "SELECT id,claim,status,updated_at FROM atoms WHERE superseded_by=? ORDER BY updated_at",
                (row["id"],))],
        }
    if name == "record_atom":
        return ingest.record(conn, args)
    if name == "record_dead_end":
        repo = args.get("repo") or (db.stats(conn)["sources"] or [""])[0]
        return db.add_dead_end(conn, repo, args.get("path", ""), args.get("tried", ""),
                               args.get("why_failed", ""), args.get("what_worked", ""),
                               args.get("conditions", ""), args.get("commit", ""))
    if name == "check_diff":
        return probes.run_check(conn, args.get("diff_text", ""))
    if name == "audit_diff":
        return audit.run(conn, args.get("diff_text", ""), atom_ids=args.get("atom_ids"),
                         record=bool(args.get("record", True)))
    if name == "record_outcome":
        return ingest.feedback(conn, args.get("atom_id", ""), args.get("outcome", ""),
                               args.get("note", ""))
    if name == "revoke_outcome":
        return db.revoke_outcome(conn, args.get("outcome_id", 0))
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


def _result_brief(name: str, payload, args: dict | None = None) -> dict:
    """只留可聚合的计数进日志，正文照旧不落盘。"""
    if not isinstance(payload, dict):
        return {}
    if name == "search":
        results = payload.get("results") or []
        tiers: dict[str, int] = {}
        for item in results:
            tier = str(item.get("tier") or "?")
            tiers[tier] = tiers.get(tier, 0) + 1
        return {
            "hits": len(results),
            # 记下"问了几条"，才能区分"只返回 2 条"和"返回 2 条是因为上限就是 2"
            "asked": int((args or {}).get("limit", 3)),
            # 命中强度：判断"返回了但都很勉强"与"库变杂"的分界，只能靠这条时间序列
            "top_score": max((float(i.get("score") or 0) for i in results), default=0.0),
            "score_version": search.SCORE_VERSION,
            "tiers": tiers,
            "dead_ends": len(payload.get("dead_ends") or []),
        }
    if name == "check_diff":
        findings = payload.get("findings") or []
        return {"findings": len(findings),
                "dead_ends": len(payload.get("dead_ends") or []),
                "related": len(payload.get("related_atoms") or []),
                # 只记命中的探针 id（不落 diff），才能按探针聚合误报率
                "probes": sorted({str(f.get("probe")) for f in findings})[:20]}
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


def _log_call(name: str, args: dict, ok: bool, ms: int, text: str,
              result: dict | None = None) -> None:
    """MCP 调用留痕；写日志失败不能影响工具调用本身。"""
    try:
        calllog.append({
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "tool": name,
            "ok": ok,
            "ms": ms,
            "args": _arg_brief(args),
            # 字符数 / 字节数 / 估算 token 三者一起记：
            # 中文的字符数与 token 数接近，英文差 4 倍，只看字符会低估英文返回体
            "result_chars": len(text),
            "result_bytes": len(text.encode("utf-8")),
            "result_tokens": calllog.estimate_tokens(text),
            # 命中率只能从结构化的计数算出来，光有字符数看不出"这次到底有没有结果"
            "result": result or {},
        })
    except OSError:
        pass


def _safe_dispatch(conn, name: str, args: dict) -> tuple[dict, bool]:
    """工具级隔离：异常与 print 都不能逃进 JSON-RPC 通道。

    MCP 的多路复用靠 stdout，任何工具内部的 print 都会撕坏协议；
    单条工具抛异常也不该结束整个会话。所以这里同时做两件事：
    把 stdout 临时收进内存，把异常转成模型可读的 error 文本。
    """
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            return _dispatch(conn, name, args), True
    except Exception as exc:  # 单次调用失败不能拖垮连接
        return {"error": f"{type(exc).__name__}: {exc}", "tool": name}, False


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
            payload, ok = _safe_dispatch(conn, name, args)
            text = json.dumps(payload, ensure_ascii=False)
            _log_call(name, args, ok, int((time.perf_counter() - started) * 1000), text,
                      _result_brief(name, payload, args))
            result = {"content": [{"type": "text", "text": text}]}
            if not ok:
                result["isError"] = True
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
