"""属性探针：不按领域分类，只按代码属性触发。

探针定义是数据（probes/*.json），不是代码里的 if 分支：
  added_any   新增行里出现这些模式之一
  require_any 同一文件里必须同时出现的上下文（如网络/持久化），否则不触发
  unless_any  同一文件的新增行里若出现这些模式之一，则不触发
  atom_hint   关联已有经验时的话题词，用来挡掉同目录里只是路径相近的无关条目
这样可以由人或 Agent 直接新增探针，不需要改程序。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from . import calllog, db, search

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# 测试、文档与快照夹具不需要生产级并发/幂等保护，默认跳过（探针可用 include_tests=true 覆盖）
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec)/|(^|/)(test_|conftest|.*_test\.|.*\.spec\.)|\.(md|json|snap)$", re.I
)


def load_probes(root: Path | None = None) -> list[dict]:
    """探针随工具走（安装目录），home 下的同名探针可覆盖。

    这样多个知识库共享同一套探针，而 home 只需存数据。
    """
    package = Path(__file__).resolve().parents[1] / "probes"
    dirs = [package] if root is None else [package, Path(root)]
    if root is None:
        dirs.append(db.home() / "probes")
    merged: dict[str, dict] = {}
    for base in dirs:
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            data.setdefault("severity", "medium")
            data.setdefault("scope", "file")
            if data.get("id") and data.get("added_any"):
                merged[data["id"]] = data
    return list(merged.values())


COMMENT = re.compile(r"^\s*(#|//|/\*|\*|<!--|--)")
OPERATOR = re.compile(r"[=(:.\[]")


def _toggle_triple(in_triple: bool, marker: str, line: str) -> tuple[bool, str]:
    """在 diff 的新文件序列里跟踪 docstring 状态（近似，但足以挡掉散文命中）。"""
    if marker:
        if line.count(marker) % 2 == 1:
            return False, ""
        return True, marker
    for m in ('"""', "'''"):
        if line.count(m) % 2 == 1:
            return True, m
    return False, ""


def parse_diff(text: str, code_only: bool = False) -> dict[str, list[tuple[int, str]]]:
    """返回 {file: [(new_lineno, added_line)]}

    code_only=True 时跳过：docstring 内、注释行、以及不含任何代码运算符的散文行。
    """
    files: dict[str, list[tuple[int, str]]] = {}
    current = ""
    lineno = 0
    in_triple = False
    marker = ""
    for raw in text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            current = target[2:] if target.startswith("b/") else ("" if target == "/dev/null" else target)
            if current:
                files.setdefault(current, [])
            in_triple, marker = False, ""
            continue
        m = HUNK.match(raw)
        if m:
            lineno = int(m.group(1))
            continue
        if raw.startswith("+"):
            line = raw[1:]
            was_inside = in_triple
            in_triple, marker = _toggle_triple(in_triple, marker, line)
            prose = was_inside or bool(COMMENT.match(line.strip())) or not OPERATOR.search(line)
            if current and not (code_only and prose):
                files[current].append((lineno, line))
            lineno += 1
        elif raw.startswith("-"):
            continue
        elif raw.startswith(" ") or raw == "":
            if raw:
                in_triple, marker = _toggle_triple(in_triple, marker, raw[1:])
            lineno += 1
    return files


def _related_for(conn, path: str, probe: dict) -> list[dict]:
    """按路径找到的经验优先；还有空位就用话题词补，补进来的标 `via=hint`。

    文档抽取的经验锚在 `.md` 上，用代码路径永远匹配不到；可它们讲的往往正是
    「这类代码该怎么写」。所以路径关联不是二选一，而是先占位、再用话题词填满——
    否则同一个目录里只要有一条噪声经验，文档经验就永远露不出来。
    """
    atoms = search.related(conn, path, limit=2, hint=probe.get("atom_hint"))
    if len(atoms) >= 2:
        return atoms
    seen = {a["id"] for a in atoms}
    for extra in related_by_hint(conn, probe, limit=2):
        if extra["id"] in seen:
            continue
        atoms.append(extra)
        if len(atoms) >= 2:
            break
    return atoms


def _hint_terms(probe: dict) -> list[str]:
    return [t.strip() for t in str(probe.get("atom_hint") or "").split("|") if t.strip()]


def _term_hit(term: str, text: str) -> bool:
    if term.isascii():
        # 英文词按整词匹配：否则 atom_hint 里的 `ok` 会把 `token`、`lookup` 也算命中
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
                         text, re.I) is not None
    return term in text


def related_by_hint(conn, probe: dict, limit: int = 2) -> list[dict]:
    """按探针的话题词找回文档经验，命中项标 `via=hint`。

    只找 `derivation='doc'`：代码经验本来就该靠路径关联，把话题词命中混进来只会
    让「网关」「状态码」这类常见词把无关条目拖进结果。同理只匹配 claim 与 mechanism，
    conditions / counter 是长句，关键词密度高，放进来几乎每条都能命中。
    """
    terms = _hint_terms(probe)
    if not terms:
        return []
    out: list[dict] = []
    for row in conn.execute(
            "SELECT * FROM atoms WHERE status!='retired' AND derivation='doc'"
            " ORDER BY status='accepted' DESC, confidence DESC, id"):
        text = " ".join(str(row[k] or "") for k in ("claim", "mechanism"))
        if not any(_term_hit(t, text) for t in terms):
            continue
        item = search.to_dict(conn, row, expand=False)
        item["via"] = "hint"
        # 跨文件、跨项目的话题命中，属于最低一级证据
        item["tier"] = search.tier_for("")
        item["score"] = 0
        out.append(item)
        if len(out) >= limit:
            break
    return out


def probe_ratios(conn, min_hits: int = 3, min_fp: int = 3,
                 min_rate: float = 0.5) -> dict:
    """每个探针的命中次数与误报率。

    命中来自 check_diff 的调用日志（只记了探针 id 列表，不落 diff 正文）；
    误报来自 `record_outcome(..., "probe_false_positive", note="probe:<id>")`。
    误报次数与误报率同时达标才建议降级：偶尔一次误报可能只是规则边界没写全。
    """
    hits: Counter = Counter()
    for call in calllog.read():
        if call.get("tool") != "check_diff":
            continue
        for pid in (call.get("result") or {}).get("probes") or []:
            hits[str(pid)] += 1
    fps: Counter = Counter()
    for row in conn.execute(
            "SELECT note FROM outcomes WHERE outcome='probe_false_positive'"):
        note = str(row["note"] or "")
        if note.startswith("probe:"):
            pid = note.split(":", 1)[1].strip()
            if pid:
                fps[pid] += 1
    out: dict[str, dict] = {}
    for pid in sorted(set(hits) | set(fps)):
        h, fp = hits[pid], fps[pid]
        rate = round(fp / h, 3) if h else None
        out[pid] = {
            "hits": h,
            "false_positives": fp,
            "rate": rate,
            "downgrade": bool(h and fp >= min_fp and rate is not None and rate >= min_rate),
        }
    return out


def noisy_probes(conn) -> dict:
    return {pid: info for pid, info in probe_ratios(conn).items() if info["downgrade"]}


def run_check(conn, diff_text: str) -> dict:
    probes = load_probes()
    files = parse_diff(diff_text, code_only=True)
    noisy = noisy_probes(conn)
    findings = []
    downgraded_hits = 0
    for path, added in files.items():
        if not added:
            continue
        for probe in probes:
            verdict = evaluate(probe, path, added)
            if not verdict["hit"]:
                continue
            if probe["id"] in noisy:
                # 误报率过高：不再当"必须看的发现"，降级成线索（可能有防护逻辑，但不构成告警）
                db.add_clue(conn, "", path, probe["id"],
                            _line_at(added, verdict["line"]), "")
                downgraded_hits += 1
                continue
            findings.append({
                "probe": probe["id"],
                "severity": probe["severity"],
                "path": path,
                "line": verdict["line"],
                "message": probe.get("message", probe["id"]),
                # 反向编译溯源：这条规则是从库内哪条经验降维来的，便于回查它为什么存在
                "from_atom": probe.get("from_atom", ""),
                "atoms": _related_for(conn, path, probe),
            })
    # 死路与探针分开报：探针说"这段代码像历史坑"，死路说"这个文件里有人试过什么且失败了"。
    # 同一文件累计 3 条失败尝试就升级为 block，理由与 projectmem 一致：
    # 一次失败可能是偶然，三次说明这条路在这个文件上确实走不通。
    counts = db.dead_end_counts(conn, list(files.keys()))
    dead = []
    for path, count in sorted(counts.items(), key=lambda x: -x[1]):
        dead.append({
            "path": path,
            "count": count,
            "severity": "block" if count >= 3 else "warn",
            "items": [dict(r) for r in db.dead_ends_for(conn, path=path, limit=3)],
        })
    return {
        "files_scanned": len(files),
        "probes": len(probes),
        "findings": findings,
        "dead_ends": dead,
        # 降级不等于删除：误报率过高的探针仍会留痕，但只以 clue 形式存在
        "downgraded": noisy,
        "downgraded_hits": downgraded_hits,
        # 没有命中时也给一句「这些文件上有哪些经验」：否则调用方拿不到 atom_id，
        # record_outcome 就永远没有可评对象，反馈闭环断在第一步。
        "related_atoms": related_atoms(conn, files) if not findings else [],
    }


def _line_at(added: list[tuple[int, str]], lineno: int | None) -> str:
    for no, line in added:
        if no == lineno:
            return line.strip()[:300]
    return ""


def evaluate(probe: dict, path: str, added: list[tuple[int, str]]) -> dict:
    """单条探针对单个文件的判定。

    返回 hit / blocked_by / line：run_check 只要结果，replay 需要知道规则卡在哪一关，
    两条路径共用同一份判定，避免「回放说该命中、真跑却不命中」这种漂移。
    """
    if TEST_PATH.search(path) and not probe.get("include_tests"):
        return {"hit": False, "blocked_by": "test_path", "line": None}
    hits = [(no, line) for no, line in added if _match_any(probe["added_any"], line)]
    if not hits:
        return {"hit": False, "blocked_by": "added_any", "line": None}
    joined = "\n".join(line for _, line in added)
    # require_any：必须在同一文件里同时出现指定上下文（如网络/IO），否则不触发
    if probe.get("require_any") and not _match_any(probe["require_any"], joined):
        return {"hit": False, "blocked_by": "require_any", "line": None}
    if _match_any(probe.get("unless_any", []), joined):
        return {"hit": False, "blocked_by": "unless_any", "line": None}
    return {"hit": True, "blocked_by": "", "line": hits[0][0]}


def related_atoms(conn, files: dict[str, list], limit: int = 4) -> list[dict]:
    """按 diff 涉及的路径找相关经验，同文件优先；测试与文档路径跳过。"""
    seen: set[str] = set()
    out: list[dict] = []
    for path in files:
        if TEST_PATH.search(path):
            continue
        for atom in search.related(conn, path, limit=2):
            if atom["id"] in seen:
                continue
            seen.add(atom["id"])
            out.append(atom)
            if len(out) >= limit:
                return out
    return out


def _match_any(patterns: list[str], text: str) -> bool:
    for pat in patterns:
        try:
            if re.search(pat, text):
                return True
        except re.error:
            continue
    return False
