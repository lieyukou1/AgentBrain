"""文档抽取：从工程文档里提炼经验，锚点是原文逐字摘录。

为什么先做文档而不是 PR 讨论：文档的锚点天生齐全——路径 + 行号 + 原文片段，
verify 用「这段摘录现在还能不能在文件里找到」就能机检，不需要人回忆当时的上下文。

代价是文档写的是结论，没有 diff 那种因果证据链，所以：
- 置信度起点低（0.4，介于 git-signal 的 0.3 与人工确认之间）；
- 只产出 candidate，仍要经 triage / 人工两键才转正；
- 反幻觉只有一道闸门：模型给的 excerpt 必须逐字出现在对应小节里，抄不出来整条作废。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import db, gitutil, llm, verify
from .enrich import GENERIC

DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt")
SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "dist", "build",
             "site-packages", "__pycache__", ".idea"}
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# 太短的小节只是标签或目录项；太长则一次塞进 prompt 既贵又容易稀释证据
MIN_SECTION = 80
MAX_SECTION = 2600
MAX_DOC_BYTES = 400_000
CONFIDENCE = 0.4

SYSTEM = (
    "你是资深工程师，负责从工程文档里提炼可复用经验。"
    "只依据给定段落，不补充段落之外的背景，不推测未写出的代码细节。"
    "输出必须是 JSON。"
)

TEMPLATE = """下面是一段工程文档（README、设计文档、运维手册或事故复盘都算）。请从中提炼可复用经验。

要求：
1. claim：一句可证伪的陈述，格式「在X条件下做Y会导致Z」。不要复述标题，不要写「文档说明了…」。
2. mechanism：因果解释（因为A，所以B），不要写口号。
3. conditions：这条经验在什么代码形态或调用路径下会被触发。
4. counter：这条经验在什么情况下不成立（反例或边界）。
5. excerpt：从下面的段落里逐字复制一句 10–120 字的原文作为证据，必须与原文完全一致，不得改写或拼接。
6. 只提炼「做什么会出错、必须怎么防」的工程经验。若段落只是在描述接口字段、状态机取值、
   业务规则或产品行为（例如「某条件下返回只读」「某字段表示当时的进度」），返回 skip：
   这类内容属于项目事实，不是可迁移的经验。
7. 一次最多提炼 2 条；宁可少提，不要凑数。

文档：{path}#{heading}
段落：
{body}

只输出 JSON，二选一：
{{"items": [{{"claim": "...", "mechanism": "...", "conditions": "...", "counter": "...", "excerpt": "..."}}]}}
{{"skip": true, "reason": "..."}}
"""


def find_docs(root: str | Path, only: str | None = None) -> list[Path]:
    root = Path(root)
    if only:
        return [Path(only)] if Path(only).is_file() else []
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in DOC_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        try:
            if p.stat().st_size > MAX_DOC_BYTES:
                continue
        except OSError:
            continue
        out.append(p)
    return sorted(out)


def split_sections(text: str) -> list[dict]:
    """按 ATX 标题切段，保留行号区间——锚点要落到行，否则复盘时得再翻一遍文档。

    代码块里的 `# 注释` 不是标题：不跟踪围栏状态会把 bash 片段切成一堆小节。
    """
    lines = text.splitlines()
    marks: list[tuple[int, str]] = []
    fence = ""
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not fence:
                fence = marker
            elif fence == marker:
                fence = ""
            continue
        if fence:
            continue
        m = HEADING.match(ln)
        if m:
            marks.append((i, m.group(2).strip()))
    out: list[dict] = []
    if not marks:
        body = "\n".join(lines).strip()
        return [{"heading": "", "start": 1, "end": len(lines),
                 "text": body[:MAX_SECTION]}] if len(body) >= MIN_SECTION else []
    for idx, (start, heading) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        body = "\n".join(lines[start + 1:end]).strip()
        if len(body) < MIN_SECTION:
            continue
        out.append({"heading": heading, "start": start + 1, "end": end,
                    "text": body[:MAX_SECTION]})
    return out


def validate_item(item: dict, section_text: str) -> tuple[bool, str]:
    """机检：长度、泛化话术，以及「摘录必须抄得出原文」。

    最后一条是这套抽取唯一的防幻觉机制——模型可以编因果，但编不出原文里没有的句子。
    """
    claim = str(item.get("claim", "")).strip()
    mechanism = str(item.get("mechanism", "")).strip()
    conditions = str(item.get("conditions", "")).strip()
    counter = str(item.get("counter", "")).strip()
    excerpt = str(item.get("excerpt", "")).strip()
    if len(claim) < 15 or len(claim) > 220:
        return False, "claim 长度不合适"
    if len(mechanism) < 12:
        return False, "mechanism 过短"
    if len(conditions) < 8:
        return False, "conditions 缺失"
    if len(counter) < 8:
        return False, "counter 缺失"
    for word in GENERIC:
        if word in claim:
            return False, f"含泛化话术「{word}」"
    if len(excerpt) < 10 or len(excerpt) > 200:
        return False, "excerpt 长度不合适"
    if excerpt not in section_text:
        return False, "excerpt 无法在原文中逐字找到（疑似改写或幻觉）"
    return True, "ok"


def _duplicate(conn, root: str, claim: str, rel: str, excerpt: str) -> bool:
    if conn.execute("SELECT 1 FROM atoms WHERE repo=? AND claim=? LIMIT 1",
                    (root, claim)).fetchone():
        return True
    return conn.execute(
        "SELECT 1 FROM anchors WHERE path=? AND excerpt=? LIMIT 1", (rel, excerpt)
    ).fetchone() is not None


def project_root(path: Path) -> Path:
    """锚点归属取项目根，不是扫描目录。

    否则 `doc-ingest <repo>/docs` 会让文档经验挂在 `<repo>/docs` 这个"仓库"下，
    与同一项目的代码经验割裂成两个 repo，检索、去重、边都连不起来。
    """
    code, out = gitutil.git(path, "rev-parse", "--show-toplevel")
    return Path(out.strip()) if code == 0 and out.strip() else path


def _rel(doc: Path, base: Path, fallback: Path) -> str:
    """文档相对归属根（项目根）的路径；项目根不是它的祖先时退回扫描根。"""
    for root in (base, fallback):
        try:
            return doc.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
    return doc.name


def _already_seen(conn, rel: str, section: dict) -> bool:
    """同一个文件、同一行区间已经有未退休的锚点，说明这一节处理过。

    去重必须发生在调用模型之前，否则重跑一次就把整本文档再买一遍。
    """
    return conn.execute(
        "SELECT 1 FROM anchors a JOIN atoms t ON t.id=a.atom_id"
        " WHERE a.path=? AND a.line_start=? AND a.line_end=? AND t.status!='retired' LIMIT 1",
        (rel, section["start"], section["end"]),
    ).fetchone() is not None


def run(conn, root: str, only: str | None = None, limit: int | None = None,
        dry_run: bool = False, model: str = llm.DEFAULT_MODEL, client=None,
        sleep_s: float = 0.4, progress=None, repo: str | None = None,
        skip_seen: bool = False) -> dict:
    root_path = Path(root).resolve()
    base = Path(repo).resolve() if repo else project_root(root_path)
    docs = find_docs(root_path, only=only)
    jobs: list[tuple[str, dict]] = []
    for doc in docs:
        try:
            text = doc.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = _rel(doc, base, root_path)
        for section in split_sections(text):
            jobs.append((rel, section))
    if skip_seen:
        fresh = [job for job in jobs if not _already_seen(conn, job[0], job[1])]
        seen_skipped = len(jobs) - len(fresh)
        jobs = fresh
    else:
        seen_skipped = 0
    selected = jobs[:limit] if limit else jobs

    stats = {"root": str(root_path), "repo": str(base), "docs": len(docs),
             "sections": len(jobs), "selected": len(selected), "seen_skipped": seen_skipped,
             "created": 0, "rejected": 0, "skipped": 0,
             "duplicates": 0, "errors": 0, "dry_run": dry_run, "details": []}
    for rel, section in selected:
        prompt = TEMPLATE.format(path=rel, heading=section["heading"] or "(无标题)",
                                 body=section["text"])
        try:
            payload = (client or llm.chat_json)(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": prompt}], model=model)
        except Exception as exc:  # 单段失败不阻断整批
            stats["errors"] += 1
            item = {"path": rel, "heading": section["heading"], "verdict": "error",
                    "reason": f"{type(exc).__name__}: {exc}"[:120]}
            stats["details"].append(item)
            if progress:
                progress(item)
            continue

        if payload.get("skip"):
            stats["skipped"] += 1
            item = {"path": rel, "heading": section["heading"], "verdict": "skipped",
                    "reason": str(payload.get("reason", ""))[:120]}
            stats["details"].append(item)
            if progress:
                progress(item)
            continue

        items = payload.get("items")
        if not isinstance(items, list):
            items = [payload] if payload.get("claim") else []
        if not items:
            stats["rejected"] += 1
            item = {"path": rel, "heading": section["heading"], "verdict": "rejected",
                    "reason": "模型未给出条目"}
            stats["details"].append(item)
            if progress:
                progress(item)
            continue

        for entry in items[:2]:
            ok, reason = validate_item(entry, section["text"])
            if not ok:
                stats["rejected"] += 1
                item = {"path": rel, "heading": section["heading"],
                        "verdict": "rejected", "reason": reason,
                        "claim": str(entry.get("claim", ""))[:80]}
                stats["details"].append(item)
                if progress:
                    progress(item)
                continue
            claim = str(entry["claim"]).strip()
            excerpt = str(entry["excerpt"]).strip()
            if _duplicate(conn, str(base), claim, rel, excerpt):
                stats["duplicates"] += 1
                item = {"path": rel, "heading": section["heading"],
                        "verdict": "duplicate", "claim": claim[:80]}
                stats["details"].append(item)
                if progress:
                    progress(item)
                continue

            stats["created"] += 1
            item = {"path": rel, "heading": section["heading"],
                    "verdict": "created", "claim": claim[:80]}
            stats["details"].append(item)
            if progress:
                progress(item)
            if dry_run:
                continue
            aid = db.add_atom(conn, {
                "claim": claim,
                "mechanism": str(entry.get("mechanism", "")).strip(),
                "conditions": str(entry.get("conditions", "")).strip(),
                "counter": str(entry.get("counter", "")).strip(),
                "derivation": "doc",
                "confidence": CONFIDENCE,
                "repo": str(base),
                "commit_sha": "",
            })
            db.add_anchor(conn, aid, "file", path=rel, line_start=section["start"],
                          line_end=section["end"], excerpt=excerpt)
            verify.verify_atom(conn, db.get_atom(conn, aid))
        if sleep_s:
            time.sleep(sleep_s)
    return stats
