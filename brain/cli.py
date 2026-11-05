"""命令行入口。零第三方依赖。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import (  # noqa: F401
    audit, brief, db, doc_ingest, enrich, evaluate, funnel, hooks, ingest, link, mine,
    mine_pr, probes, refine, replay, rules, search, verify,
)


def _out(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _short(text: str, n: int = 88) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def cmd_add_source(conn, args) -> int:
    p = str(Path(args.path).resolve())
    conn.execute("INSERT OR IGNORE INTO sources(path,added_at) VALUES(?,?)", (p, db.now()))
    conn.commit()
    _out({"added": p, "sources": db.stats(conn)["sources"]})
    return 0


def cmd_mine(conn, args) -> int:
    if args.from_queue:
        entries = hooks.read_queue()
        if not entries:
            _out({"queued": 0, "results": [], "note": "队列为空；先 brain hook install"})
            return 0
        by_repo: dict[str, list[dict]] = {}
        for entry in entries:
            by_repo.setdefault(entry["repo"], []).append(entry)
        results, done = [], []
        for repo, items in by_repo.items():
            result = mine.mine_repo(conn, repo, limit=args.limit,
                                    shas=[i["sha"] for i in items])
            results.append(result)
            if "error" not in result:
                done.extend(items)
        _out({"queued": len(entries), "consumed": hooks.consume(done),
              "results": results, "stats": db.stats(conn)})
        return 0
    repos = [args.repo] if args.repo else db.stats(conn)["sources"]
    if not repos:
        _out({"error": "没有种子仓库，先运行：brain add-source <path>"})
        return 2
    results = [mine.mine_repo(conn, r, limit=args.limit, since=args.since) for r in repos]
    _out({"results": results, "stats": db.stats(conn)})
    return 0


def cmd_mine_pr(conn, args) -> int:
    result = mine_pr.mine(conn, args.repo, limit=args.limit, dry_run=args.dry_run,
                          token=args.token)
    _out(result)
    return 0 if "error" not in result else 2


def cmd_hook(conn, args) -> int:
    if args.uninstall:
        _out(hooks.uninstall(args.repo))
    else:
        _out(hooks.install(args.repo, pre_commit=args.pre_commit))
    return 0


def cmd_add(conn, args) -> int:
    result = ingest.record(conn, {
        "claim": args.claim,
        "mechanism": args.mechanism,
        "conditions": args.conditions,
        "counter": args.counter,
        "repo": args.repo,
        "commit": args.commit,
        "path": args.path,
        "line_start": args.line_start,
        "line_end": args.line_end,
        "excerpt": args.excerpt,
        "supersedes": args.supersedes,
    })
    _out(result)
    return 0 if "error" not in result else 2


def cmd_revoke(conn, args) -> int:
    result = db.revoke_outcome(conn, args.outcome_id)
    _out(result)
    return 0 if "error" not in result else 2


def cmd_dead_end(conn, args) -> int:
    repo = args.repo
    if not repo:
        sources = db.stats(conn)["sources"]
        repo = sources[0] if sources else ""
    result = db.add_dead_end(conn, repo, args.path, args.tried, args.why,
                             args.worked, args.conditions, args.commit)
    _out(result)
    return 0 if "error" not in result else 2


def cmd_dead_ends(conn, args) -> int:
    rows = db.dead_ends_for(conn, path=args.path, repo=args.repo, limit=args.limit)
    _out({"count": len(rows), "items": [dict(r) for r in rows]})
    return 0


def cmd_brief(conn, args) -> int:
    result = brief.build(conn, repo=args.repo, budget=args.budget)
    if args.json:
        _out(result)
    else:
        print(result["text"])
    return 0


def cmd_init_docs(conn, args) -> int:
    result = rules.apply_to(args.repo, write=args.write)
    _out(result)
    return 0 if "error" not in result else 2


def cmd_prune(conn, args) -> int:
    if args.clues:
        _out(refine.clear_clues(conn))
        return 0
    if not args.guard_only:
        _out({"error": "需要指定 --guard-only 或 --clues"})
        return 2
    _out(refine.retire_guard_only(conn))
    return 0


def cmd_clues(conn, args) -> int:
    rows = list(conn.execute(
        "SELECT * FROM clues WHERE status='pending' ORDER BY id DESC LIMIT ?", (args.limit,)
    ))
    if args.json:
        _out([dict(r) for r in rows])
        return 0
    if not rows:
        print("暂无探针线索。")
        return 0
    print(f"探针线索 {len(rows)} 条（这些是「可能藏着防护逻辑」的代码位置，不是知识）：\n")
    for r in rows:
        print(f"#{r['id']}  [{r['token']}]  {r['path']}")
        print(f"   {_short(r['excerpt'], 120)}")
        print(f"   commit {r['commit_sha'][:12]}  repo {r['repo']}")
    return 0


def cmd_enrich(conn, args) -> int:
    def progress(item: dict) -> None:
        tag = item["verdict"]
        text = item.get("claim") or item.get("reason", "")
        print(f"[{tag}] {item['id']}  {_short(text, 100)}", flush=True)

    result = enrich.run(
        conn,
        limit=args.limit,
        model=args.model,
        dry_run=args.dry_run,
        sleep_s=args.sleep,
        only_id=args.id,
        progress=progress,
        refill=args.refill,
    )
    if args.json:
        _out(result)
    else:
        print(f"\n选中 {result['selected']}｜接受 {result['accepted']}｜拒绝 {result['rejected']}"
              f"（其中 skip {result['skipped']}）｜错误 {result['errors']}"
              f"｜接受率 {result['accept_rate']}")
    return 0 if result["errors"] == 0 else 1


def cmd_doc_ingest(conn, args) -> int:
    def progress(item: dict) -> None:
        text = item.get("claim") or item.get("reason", "")
        where = f"{item['path']}#{item['heading']}" if item.get("heading") else item["path"]
        print(f"[{item['verdict']}] {where}  {_short(text, 100)}", flush=True)

    result = doc_ingest.run(
        conn, args.path, only=args.only, limit=args.limit, dry_run=args.dry_run,
        model=args.model, sleep_s=args.sleep, progress=progress,
        repo=args.repo, skip_seen=args.skip_seen,
    )
    if args.json:
        _out(result)
        return 0
    print(f"\n文档 {result['docs']} 个｜小节 {result['sections']}｜处理 {result['selected']}"
          f"｜写入 {result['created']}｜拒绝 {result['rejected']}（其中 skip {result['skipped']}）"
          f"｜重复 {result['duplicates']}｜已处理跳过 {result['seen_skipped']}"
          f"｜错误 {result['errors']}")
    return 0 if result["errors"] == 0 else 1


def cmd_verify(conn, args) -> int:
    _out(verify.verify_all(conn, only_candidates=args.candidates_only))
    return 0


def cmd_review(conn, args) -> int:
    rows = list(conn.execute(
        "SELECT * FROM atoms WHERE status='candidate' ORDER BY confidence DESC, created_at DESC LIMIT ?",
        (args.limit,),
    ))
    if args.json:
        _out([{**{k: r[k] for k in r.keys()},
               "anchors": [dict(a) for a in db.anchors_of(conn, r["id"])]} for r in rows])
        return 0
    if not rows:
        print("没有待审候选。用 brain mine 先挖掘。")
        return 0
    print(f"待审候选 {len(rows)} 条（good=采纳并加权，bad=退休）：\n")
    for r in rows:
        anchors = db.anchors_of(conn, r["id"])
        res = [a["resolved"] for a in anchors]
        rate = f"{sum(1 for x in res if x == 1)}/{len(res)}"
        print(f"[{r['id']}] conf={r['confidence']:.2f} anchors={rate} {r['derivation']}")
        print(f"  {_short(r['claim'], 160)}")
        for a in anchors[:3]:
            print(f"  - {a['kind']} {a['path'] or a['commit_sha'][:10]} {_short(a['excerpt'], 60)}")
        print()
    return 0


def cmd_rate(conn, args) -> int:
    result = ingest.review(conn, args.id, args.verdict, args.note)
    _out(result)
    if "error" not in result:
        return 0
    return 3 if result.get("reason") == "unresolved_anchor" else 2


def cmd_outcome(conn, args) -> int:
    result = ingest.feedback(conn, args.id, args.outcome, args.note)
    _out(result)
    return 0 if "error" not in result else 2


def cmd_search(conn, args) -> int:
    results = search.search_atoms(conn, args.query, limit=args.limit,
                                  fallback=args.any, repo=args.repo,
                                  freshness=args.freshness, stack=args.stack)
    dead = search.dead_ends(conn, args.query)
    if args.json:
        _out({"results": results, "dead_ends": dead})
        return 0
    if not results and not dead:
        print("无命中。加 --any 可查看高置信条目（但那不是检索结果）。")
        return 0
    for r in results:
        print(f"[{r['id']}] conf={r['confidence']:.2f} score={r.get('score', 0)} "
              f"{r.get('tier', '?')} {r.get('freshness', '?')} {r['status']}  "
              f"{_short(r['claim'], 140)}")
        for a in r["anchors"][:2]:
            print(f"   - {a['kind']} {a['path'] or a['commit']} {_short(a['excerpt'], 60)}")
        for e in r.get("linked", [])[:2]:
            via = f" via {'、'.join(e['via'][:2])}" if e.get("via") else ""
            print(f"   ↔ {e['rel']}({e['weight']:.0f}){via} [{e['id']}] {_short(e['claim'], 70)}")
    if dead:
        print("\n死路（试过并失败，不要再试）：")
        for d in dead:
            print(f"   [{d['path'] or '未标注文件'}] 试过：{_short(d['tried'], 90)}")
            if d["why_failed"]:
                print(f"      失败原因：{_short(d['why_failed'], 90)}")
            if d["what_worked"]:
                print(f"      有效做法：{_short(d['what_worked'], 90)}")
    return 0


def cmd_explain(conn, args) -> int:
    row = db.get_atom(conn, args.id)
    if row is None:
        _out({"error": "atom 不存在", "id": args.id})
        return 2
    _out({
        **{k: row[k] for k in row.keys()},
        "freshness": search.freshness_of(conn, args.id),
        "anchors": [dict(a) for a in db.anchors_of(conn, args.id)],
        "edges": [dict(e) for e in conn.execute(
            "SELECT rel,dst,weight FROM edges WHERE src=? ORDER BY weight DESC", (args.id,))],
        "outcomes": [dict(o) for o in conn.execute(
            "SELECT id,outcome,note,revoked,created_at FROM outcomes WHERE atom_id=? ORDER BY id",
            (args.id,))],
        "supersedes": [dict(r) for r in conn.execute(
            "SELECT id,claim,status,updated_at FROM atoms WHERE superseded_by=? ORDER BY updated_at",
            (args.id,))],
    })
    return 0


def cmd_check(conn, args) -> int:
    text = Path(args.diff).read_text(encoding="utf-8", errors="replace") if args.diff else sys.stdin.read()
    result = probes.run_check(conn, text)
    if args.json:
        _out(result)
        return 0
    print(f"扫描 {result['files_scanned']} 个文件，{result['probes']} 个探针，发现 {len(result['findings'])} 条：")
    for f in result["findings"]:
        print(f"\n[{f['severity']}] {f['probe']}  {f['path']}:{f['line']}")
        print(f"  {f['message']}")
        if f.get("from_atom"):
            print(f"  ← 编译自经验 {f['from_atom']}")
        for a in f["atoms"]:
            tag = "（话题关联，非同一文件）" if a.get("via") == "hint" else ""
            print(f"  ← 相关经验 [{a['id']}] {_short(a['claim'], 100)}{tag}")
        if not f["atoms"]:
            print("  ← 库内暂无对应经验（该探针本身就是待沉淀的线索）")
    if result.get("downgraded_hits"):
        print(f"\n另有 {result['downgraded_hits']} 条命中来自已降级探针"
              f"（{', '.join(sorted(result.get('downgraded', {})))}），只记线索、不告警。")
    if result["dead_ends"]:
        # C3：三段一起给，读者才能直接回答「试过什么、为什么不行、该怎么做」，
        # 只写一句「试过不行」等于没记
        print("\n这些文件上试过并失败的做法：")
        for d in result["dead_ends"]:
            print(f"\n[{d['severity']}] {d['path']}（累计 {d['count']} 次失败）")
            for item in d["items"]:
                print(f"  试过：{_short(item['tried'], 120)}")
                print(f"  为什么不行：{_short(item['why_failed'], 120) or '未记录'}")
                print(f"  有效做法：{_short(item['what_worked'], 120) or '未记录'}")
    if result["related_atoms"]:
        print("\n这些文件上的既有经验（可用于 record_outcome）：")
        for a in result["related_atoms"]:
            print(f"  [{a['id']}] conf={a['confidence']:.2f} {_short(a['claim'], 100)}")
    return 0


def cmd_triage(conn, args) -> int:
    result = refine.triage(conn, apply=args.apply)
    if args.json:
        _out(result)
        return 0
    counts = result["counts"]
    print(f"候选分流（apply={'是' if result['applied'] else '否，只报告'}）：")
    print(f"  退休 {counts['retire']}｜采纳 {counts['accept']}"
          f"｜待补富化 {counts['refill']}｜需人工 {counts['hold']}")
    for name, title in (("accept", "采纳"), ("refill", "待补富化"),
                        ("hold", "需人工"), ("retire", "退休")):
        items = result["buckets"][name]
        if not items:
            continue
        print(f"\n[{title}] {len(items)} 条，示例：")
        for item in items[:5]:
            print(f"  {item['id']}  {item['reason']}｜{_short(item['claim'], 80)}")
    if not result["applied"]:
        print("\n加 --apply 落库（退休 + 采纳，refill/hold 保持原状）")
    return 0


def cmd_revisions(conn, args) -> int:
    where, params = [], []
    if args.id:
        where.append("atom_id=?")
        params.append(args.id)
    if args.verdict:
        where.append("verdict=?")
        params.append(args.verdict)
    sql = "SELECT * FROM revisions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(args.limit)
    rows = list(conn.execute(sql, params))
    if args.json:
        _out([dict(r) for r in rows])
        return 0
    if not rows:
        print("没有富化记录。")
        return 0
    for row in rows:
        print(f"#{row['id']} [{row['verdict']}] {row['atom_id']}  {row['created_at']}")
        print(f"  old claim : {_short(row['old_claim'], 110)}")
        print(f"  new claim : {_short(row['new_claim'], 110)}")
        if row["old_conditions"] != row["new_conditions"]:
            print(f"  old cond  : {_short(row['old_conditions'], 80)}")
            print(f"  new cond  : {_short(row['new_conditions'], 80)}")
        if row["reason"]:
            print(f"  reason    : {row['reason']}")
        print()
    return 0


def cmd_revert(conn, args) -> int:
    result = refine.revert_revision(conn, args.revision)
    _out(result)
    return 0 if "error" not in result else 2


def cmd_audit(conn, args) -> int:
    text = Path(args.diff).read_text(encoding="utf-8", errors="replace") if args.diff else sys.stdin.read()
    result = audit.run(conn, text, atom_ids=args.atom or None, model=args.model,
                       record=not args.no_record)
    if args.json:
        _out(result)
        return 0
    print(f"候选经验 {result['candidates']} 条｜判定 {len(result['results'])} 条"
          f"｜证据不成立 {result['rejected_evidence']} 条"
          f"｜记入疑似命中 {result['recorded']} 条")
    if result.get("note"):
        print(f"  {result['note']}")
    for item in result["results"]:
        print(f"\n[{item['verdict']}] {item['atom_id']}  {_short(item['claim'], 100)}")
        if item.get("evidence"):
            print(f"  证据: {_short(item['evidence'], 120)}")
        if item.get("reason"):
            print(f"  理由: {_short(item['reason'], 120)}")
    return 0


def cmd_link(conn, args) -> int:
    _out(link.rebuild(conn))
    return 0


def cmd_funnel(conn, args) -> int:
    data = funnel.report(conn, days=args.days)
    if args.json:
        _out(data)
        return 0
    k, p, u, f = data["knowledge"], data["processing"], data["usage"], data["feedback"]
    print("知识层：" + "｜".join(f"{name} {n}" for name, n in sorted(k["by_status"].items())))
    print(f"        锚点 {k['anchors']} 个，解决率 {k['anchor_resolution_rate']}"
          f"｜边 {k['edges']}｜待处理线索 {k['pending_clues']}")
    graded = p["enrich_accepted"] + p["enrich_rejected"]
    print(f"加工层：富化接受 {p['enrich_accepted']}/{graded}（{p['enrich_accept_rate']}）"
          "｜来源 " + " ".join(f"{d}={n}" for d, n in sorted(p["by_derivation"].items())))
    print(f"使用层（近 {u['window_days']} 天）：调用 {u['calls']} 次，成功率 {u['ok_rate']}，"
          f"p50 {u['ms_p50']}ms")
    if u["by_tool"]:
        print("        " + " ".join(f"{t}={n}" for t, n in sorted(u["by_tool"].items())))
    rate = lambda v: "无可判定记录" if v is None else f"{v}"
    print(f"        search 命中率 {rate(u['search_hit_rate'])}"
          f"｜check_diff 命中率 {rate(u['check_hit_rate'])}"
          f"｜旧日志无计数 {u['unjudged']} 次")
    if u["search_graded"]:
        print(f"        检索强度（{u['search_graded']} 次有分数）："
              f"top_score p50 {u['search_top_score_p50']}｜p90 {u['search_top_score_p90']}"
              f"｜满额返回 {u['search_full_rate']}"
              "｜分层 " + " ".join(f"{k}={v}" for k, v in sorted(u["search_tiers"].items())))
    if u["search_tokens_p50"] is not None:
        print(f"        检索成本：约 {u['search_tokens_p50']} token/次（p50）"
              f"｜p90 {u['search_tokens_p90']}"
              f"｜{u['search_bytes_p50']} 字节/次（p50）"
              f"｜平均返回 {u['search_returned_avg']} 条")
    print(f"反馈层：outcome {f['outcomes']} 条，validated {f['validated']}"
          f"（{f['validated_rate']}），其中机器批量判定 {f['auto_graded']} 条"
          f"，取代 {f['superseded']} 条")
    print(f"        audit 疑似命中 {f['audit_hits']} 次，覆盖 {f['audit_atoms']} 条经验")
    print(f"        真实使用反馈覆盖 {f['real_feedback_atoms']} 条经验；"
          f"accepted 里 {f['accepted_without_real_feedback']} 条只是格式合格")
    pr = data.get("probes")
    if pr:
        print(f"探针层：{pr.get('total', 0)} 条带回放来源，命中率 {pr.get('hit_rate')}"
              f"｜数据缺口 {pr.get('unrunnable', 0)}"
              f"｜记录于 {pr.get('ran_at', '?')}")
    else:
        print("探针层：还没有回放记录（运行 python -m brain replay）")
    pf = data.get("probe_feedback") or {}
    if pf:
        print("        命中/误报：" + "｜".join(
            f"{pid} {info['hits']}/{info['false_positives']}"
            + ("（已降级）" if info["downgrade"] else "")
            for pid, info in sorted(pf.items())))
    return 0


def cmd_replay(conn, args) -> int:
    result = replay.replay_all(conn, only_probe=args.probe)
    snapshot = replay.record(result)
    failed = (args.min_hit_rate is not None
              and (result["hit_rate"] is None or result["hit_rate"] < args.min_hit_rate))
    if args.json:
        _out({**result, "recorded_at": snapshot["ran_at"],
              "gate": None if args.min_hit_rate is None
              else {"min_hit_rate": args.min_hit_rate, "passed": not failed}})
        return 2 if failed else 0
    if not result["total"]:
        print("没有带来源经验的探针（检查 probes/*.json 的 from_atom）。")
        return 2 if failed else 0
    print(f"回放 {result['total']} 条探针："
          + "｜".join(f"{k} {v}" for k, v in sorted(result["by_status"].items())))
    if result["blocked_by"]:
        print("未命中原因：" + "｜".join(f"{k} {v}" for k, v in sorted(result["blocked_by"].items())))
    if result["hit_rate"] is not None:
        print(f"可回放命中率 {result['hit_rate']}（{result['by_status'].get('hit', 0)}/"
              f"{result['by_status'].get('hit', 0) + result['by_status'].get('miss', 0)}）"
              f"｜数据缺口 {result['unrunnable']} 条")
    print()
    labels = {"hit": "命中", "miss": "未命中", "no_source": "源经验缺失",
              "no_commit": "无提交锚点", "no_parent": "无父提交",
              "no_path_anchor": "无文件锚点"}
    for row in result["rows"]:
        status = labels.get(row["status"], row["status"])
        detail = row.get("blocked_by") or row.get("path") or row.get("commit", "")
        print(f"  {row['probe']:<42}{status:<8}{detail}")
    if failed:
        print(f"\n门禁未通过：命中率低于 {args.min_hit_rate}。"
              "先修规则（blocked_by 指向卡住的门槛），或修数据缺口。")
        return 2
    return 0


def cmd_eval(conn, args) -> int:
    result = evaluate.run(conn, path=args.path, limit=args.limit)
    if args.json:
        _out(result)
        return 0
    print(f"评测 {result['queries']} 条查询（有期望 {result['graded']}｜空期望 {result['negatives']}）")
    print(f"recall@1 {result['recall_at_1']}｜recall@3 {result['recall_at_3']}"
          f"｜precision@3 {result['precision_at_3']}"
          f"｜有期望却零命中 {result['zero_hit']}｜空期望误报 {result['false_positives']}")
    print(f"该空就空 {result['abstention_accuracy']}｜跨库隔离 {result['scope_isolation']}"
          f"｜失效条目 {result['stale_shown']}"
          f"｜超预算 {result['over_budget']}/{result['token_budget_bytes']}B"
          f"｜延迟 p50 {result['latency_p50_ms']}ms / p95 {result['latency_p95_ms']}ms")
    for row in result["rows"]:
        mark = ("✓" if row["hit_at_3"] else "✗") if row["expected"] else \
               ("!" if row["false_positive"] else "✓")
        print(f"  {mark} top={row['top_score']:<6} got={len(row['got'])}  {row['query']}")
    return 0


def cmd_stats(conn, args) -> int:
    _out(db.stats(conn))
    return 0


def cmd_mcp_config(conn, args) -> int:
    root = db.home()
    exe = Path(sys.executable)
    try:
        inside = exe.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        inside = False
    # 当前解释器若来自别的项目虚拟环境，写进配置会让 MCP 启动依赖那个环境，故退回 PATH 上的 python
    command = str(exe) if inside else "python"
    cfg = {
        "mcpServers": {
            "agentbrain": {
                "command": command,
                "args": ["-m", "brain", "mcp"],
                "env": {"PYTHONPATH": str(root), "BRAIN_HOME": str(root)},
            }
        }
    }
    _out(cfg)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brain", description="外置大脑：结构性信号挖掘 + 属性探针 + 证据锚点")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add-source", help="登记一个 git 仓库作为种子")
    s.add_argument("path")
    s.set_defaults(func=cmd_add_source)

    s = sub.add_parser("mine", help="从 git 结构性信号挖候选")
    s.add_argument("--repo")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--since")
    s.add_argument("--from-queue", action="store_true",
                   help="只处理 git hook 入队的提交，处理完清空队列")
    s.set_defaults(func=cmd_mine)

    s = sub.add_parser("mine-pr", help="从 GitHub PR 评论挖候选（只读 API，需 GITHUB_TOKEN）")
    s.add_argument("--repo", required=True)
    s.add_argument("--limit", type=int, default=mine_pr.DEFAULT_PRS,
                   help="扫描最近更新的多少个 PR")
    s.add_argument("--dry-run", action="store_true", help="只报告配对，不写库")
    s.add_argument("--token", default=None,
                   help="GitHub 只读 token；建议改用环境变量 GITHUB_TOKEN，避免留在 shell 历史里")
    s.set_defaults(func=cmd_mine_pr)

    s = sub.add_parser("hook", help="安装/卸载 git hook（轻量入队；可选提交前回放门禁）")
    s.add_argument("--repo", required=True)
    s.add_argument("--pre-commit", action="store_true",
                   help="同时安装 pre-commit：回放命中率低于 0.9 时拒绝提交")
    s.add_argument("--uninstall", action="store_true")
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("add", help="写入一条候选经验（必须带锚点）")
    s.add_argument("--claim", required=True)
    s.add_argument("--mechanism", default="")
    s.add_argument("--conditions", default="")
    s.add_argument("--counter", default="")
    s.add_argument("--repo", default="")
    s.add_argument("--commit", default="")
    s.add_argument("--path", default="")
    s.add_argument("--line-start", type=int, default=None)
    s.add_argument("--line-end", type=int, default=None)
    s.add_argument("--excerpt", default="")
    s.add_argument("--supersedes", default="", help="用这条新结论取代的旧 atom id")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("revoke", help="撤销一次误记的反馈（保留事件，回滚置信度）")
    s.add_argument("outcome_id", type=int)
    s.set_defaults(func=cmd_revoke)

    s = sub.add_parser("dead-end", help="记一条「试过但失败」的死路")
    s.add_argument("--tried", required=True, help="试过什么做法")
    s.add_argument("--why", default="", help="为什么失败")
    s.add_argument("--worked", default="", help="最后什么做法有效")
    s.add_argument("--conditions", default="")
    s.add_argument("--path", default="")
    s.add_argument("--commit", default="")
    s.add_argument("--repo", default="")
    s.set_defaults(func=cmd_dead_end)

    s = sub.add_parser("dead-ends", help="列出已记录的死路（可按文件/仓库过滤）")
    s.add_argument("--path", default=None)
    s.add_argument("--repo", default=None)
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_dead_ends)

    s = sub.add_parser("brief", help="有界快照：会话启动时该看的一页")
    s.add_argument("--repo", default=None)
    s.add_argument("--budget", type=int, default=brief.BRIEF_CHARS)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("init-docs", help="检查/刷新 AGENTS.md 里的经验库使用约定")
    s.add_argument("--repo", required=True)
    s.add_argument("--write", action="store_true", help="实际写入；不加只报告状态")
    s.set_defaults(func=cmd_init_docs)

    s = sub.add_parser("prune", help="把早期误升格的条目退回（保留历史）")
    s.add_argument("--guard-only", action="store_true")
    s.add_argument("--clues", action="store_true", help="清空待处理线索，便于用收紧后的过滤器重跑")
    s.set_defaults(func=cmd_prune)

    s = sub.add_parser("clues", help="查看探针线索（guard-only 降级后的去处）")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_clues)

    s = sub.add_parser("enrich", help="用 LLM 把提交摘要升级为 claim/mechanism/counter")
    s.add_argument("--limit", type=int, default=None)
    s.add_argument("--id", default=None)
    s.add_argument("--refill", action="store_true",
                   help="只重跑 conditions 为占位话术的已富化条目")
    s.add_argument("--model", default="deepseek-chat")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--sleep", type=float, default=0.4)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_enrich)

    s = sub.add_parser("doc-ingest", help="从工程文档抽取经验（锚点=原文逐字摘录）")
    s.add_argument("path", help="文档所在目录；配 --only 时可直接给单个文件")
    s.add_argument("--only", default=None, help="只处理某一个文件")
    s.add_argument("--repo", default=None,
                   help="锚点归属的项目根；默认取扫描目录所在的 git 根")
    s.add_argument("--skip-seen", action="store_true",
                   help="跳过已处理过的小节（同文件同行区间已有未退休锚点），避免重复调用模型")
    s.add_argument("--limit", type=int, default=None, help="最多处理多少个小节")
    s.add_argument("--model", default="deepseek-chat")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--sleep", type=float, default=0.4)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_doc_ingest)

    s = sub.add_parser("verify", help="校验锚点是否仍可从仓库解析")
    s.add_argument("--candidates-only", action="store_true")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("review", help="列出待审候选（人工两键）")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("rate", help="给候选打分：good 采纳 / bad 退休")
    s.add_argument("id")
    s.add_argument("verdict", choices=["good", "bad"])
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_rate)

    s = sub.add_parser("outcome", help="记一次真实使用反馈（分档信号）")
    s.add_argument("id")
    s.add_argument("outcome", choices=sorted(db.FEEDBACK_DELTAS))
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_outcome)

    s = sub.add_parser("search", help="检索知识")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--repo", default=None, help="只在该仓库内检索")
    s.add_argument("--freshness", default=None, choices=["ok", "partial", "unverified"],
                   help="只看指定证据状态的条目")
    s.add_argument("--stack", default=None,
                   help="当前项目技术栈（python/node/java/go/rust/docker）；省略时按 repo/cwd 自动识别")
    s.add_argument("--json", action="store_true")
    s.add_argument("--any", action="store_true", help="无命中时退化为按置信度取前 N 条")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("explain", help="查看单条知识的证据与反馈")
    s.add_argument("id")
    s.set_defaults(func=cmd_explain)

    s = sub.add_parser("check", help="对 diff 跑属性探针")
    s.add_argument("--diff", help="diff 文件；省略则从 stdin 读")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("audit", help="用历史经验对 diff 做语义审查（结论须附原行）")
    s.add_argument("--diff", help="diff 文件；省略则从 stdin 读")
    s.add_argument("--atom", action="append", help="指定对照的经验 id，可重复")
    s.add_argument("--no-record", action="store_true", help="只输出结论，不记疑似命中")
    s.add_argument("--model", default="deepseek-chat")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("triage", help="按机检规则给候选分流（退休/采纳/待补富化/人工）")
    s.add_argument("--apply", action="store_true", help="落库：执行退休与采纳")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_triage)

    s = sub.add_parser("revisions", help="查看富化留底（old vs new）")
    s.add_argument("--id", default=None, help="只看某条 atom")
    s.add_argument("--verdict", default=None, choices=["accepted", "rejected", "reverted"])
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_revisions)

    s = sub.add_parser("revert", help="把某次富化回退到留底文本")
    s.add_argument("revision", type=int)
    s.set_defaults(func=cmd_revert)

    s = sub.add_parser("link", help="按共享锚点重建经验之间的边（幂等）")
    s.set_defaults(func=cmd_link)

    s = sub.add_parser("stats", help="库健康度")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("eval", help="检索回归：真实查询 + 人工标注的期望条目")
    s.add_argument("--path", default=None, help="评测集路径，默认 eval/queries.json")
    s.add_argument("--limit", type=int, default=5)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("funnel", help="使用漏斗：知识→加工→使用→反馈")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_funnel)

    s = sub.add_parser("replay", help="回放探针：拿它去认自己来源的坏代码，检验自洽")
    s.add_argument("--probe", default=None, help="只回放某一条探针")
    s.add_argument("--min-hit-rate", type=float, default=None,
                   help="门禁：可回放探针的命中率低于该值时以退出码 2 失败（建议 0.9）")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_replay)

    s = sub.add_parser("mcp-config", help="输出 MCP 客户端配置片段")
    s.set_defaults(func=cmd_mcp_config)

    s = sub.add_parser("mcp", help="以 MCP stdio 服务方式运行")
    s.set_defaults(func=lambda conn, args: _run_mcp())
    return p


def _run_mcp() -> int:
    from .mcp import serve
    return serve()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    conn = db.connect()
    try:
        return args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
