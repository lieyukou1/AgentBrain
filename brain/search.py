"""检索：LIKE 多词 AND + 一跳图扩展。

刻意不使用向量库：在几千条规模上，LIKE 足够快，而且不会给出"看起来对但没出处"的结果。
"""
from __future__ import annotations

import os
import re

from . import db
from .link import NON_CODE_SUFFIXES

CHUNK = re.compile(r"[A-Za-z_][A-Za-z0-9_]+|[\u4e00-\u9fff]+")
STOP = {"self", "true", "false", "none", "null", "return", "import", "class", "public",
        "private", "static", "final", "const", "async", "await", "with", "from"}
# 命中排序：claim 命中比 counter 命中更能说明「这条讲的就是你要找的」
FIELD_WEIGHT = {"claim": 3.0, "mechanism": 2.0, "conditions": 2.0, "counter": 1.5}
STATUS_BONUS = {"accepted": 1.5}
# 边按证据强度取前几条，避免同一文件上的经验把 linked 撑成一长串
LINKED_LIMIT = 5
MAX_TOKENS = 10


def tokenize(query: str) -> list[str]:
    """拆检索词：英文标识符整词保留，中文按 2-gram 切。

    中文不分词时「幂等 租约 续租」会被当成三个完整词做 AND，而库里几乎不会有
    一条记录同时逐字包含它们；切 2-gram 后相邻片段能各自命中，再靠覆盖度排序。
    """
    out: list[str] = []
    seen: set[str] = set()
    for chunk in CHUNK.findall(query or ""):
        if chunk[0].isascii():
            parts = [] if len(chunk) < 2 or chunk.lower() in STOP else [chunk]
        elif len(chunk) == 1:
            parts = []  # 单字没有区分度
        elif len(chunk) == 2:
            parts = [chunk]
        else:
            parts = [chunk[i:i + 2] for i in range(len(chunk) - 1)]
        for token in parts:
            if token in seen:
                continue
            seen.add(token)
            out.append(token)
            if len(out) >= MAX_TOKENS:
                return out
    return out


def min_hits(token_count: int) -> int:
    """长查询放宽到覆盖三分之一，短查询仍要求全中。

    两词查询放宽成 OR 会把「租约 不存在」这种否定查询变成一堆无关结果，
    精度比召回更值钱；三个词以上才放宽。中文 2-gram 是密集的：命中 3 个
    相邻 bigram 大约等于有 4 个连续字重合，所以门槛按词数的三分之一设。
    """
    if token_count <= 2:
        return token_count
    return max(2, (token_count + 2) // 3)


def score(row, tokens: list[str]) -> tuple[float, int]:
    """命中强度分与命中词数：字段权重 × 命中词 + 覆盖度 + 已采纳加成 + 置信度。

    只用 LIKE 过滤时，四词全中与只中一词在 confidence 相同时会并排，
    排序退化成插入顺序；这一步把「命中得多、命中得靠前」拉回排序依据。
    """
    total = 0.0
    covered = 0
    for token in tokens:
        hit = False
        for field, weight in FIELD_WEIGHT.items():
            if token in str(row[field] or ""):
                total += weight
                hit = True
        covered += 1 if hit else 0
    if tokens:
        total += 2.0 * covered / len(tokens)
    return total + STATUS_BONUS.get(row["status"], 0.0) + float(row["confidence"]), covered


def _norm_repo(value: str) -> str:
    """仓库路径归一：库里同时存在 `E:\\work\\proj-a`、`E:/work/proj-a/` 这几种写法。"""
    text = str(value or "").strip()
    return os.path.normcase(os.path.abspath(text)) if text else ""


def search_atoms(conn, query: str, limit: int = 10, fallback: bool = True,
                 repo: str | None = None) -> list[dict]:
    """fallback=True 时，无命中会退化为"按置信度取前 N 条"；探针关联必须用 fallback=False，
    否则会把不相关的经验硬塞给用户。

    repo 非空时只在该仓库内检索：多项目库里同一个词（如「幂等」）会同时命中几个项目，
    不区分仓库的话，最有用的那条会被别的项目的条目挤掉。

    过滤与排序都在内存里做：词数很少、表也不大，一条 SQL 拼 OR 反而既慢又容易漏。
    """
    tokens = tokenize(query)
    target = _norm_repo(repo) if repo else ""
    rows: list = []
    if tokens:
        need = min_hits(len(tokens))
        scored = []
        for row in conn.execute("SELECT * FROM atoms WHERE status!='retired'"):
            if target and _norm_repo(row["repo"]) != target:
                continue
            value, covered = score(row, tokens)
            if covered >= need:
                scored.append((value, row))
        # 同分再按置信度、id 排，保证结果稳定可复现
        scored.sort(key=lambda pair: (-pair[0], -float(pair[1]["confidence"]), pair[1]["id"]))
        rows = [row for _, row in scored[:int(limit)]]
    if not rows and fallback:
        pool = [r for r in conn.execute(
            "SELECT * FROM atoms WHERE status!='retired' ORDER BY confidence DESC")]
        if target:
            pool = [r for r in pool if _norm_repo(r["repo"]) == target]
        rows = pool[:int(limit)]
    return [to_dict(conn, r, expand=True) for r in rows]


def _like(text: str) -> str:
    """转义 LIKE 元字符：文件名里的 `_` 是通配符，而代码文件名几乎都带下划线。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def related(conn, path: str, limit: int = 2, hint: str | None = None) -> list[dict]:
    """探针命中后找相关经验，按证据强度排序。

    评分：同一文件 3 分；文件名词干的罕见标识符（df≤5）重合 2 分；同目录 1 分。
    可叠加。若探针给了 atom_hint，只保留文本命中的条目。
    都不满足就返回空——宁可说"库内暂无"，也不塞不相关的内容。
    """
    base = path.rsplit("/", 1)[-1]
    folder = (path.rsplit("/", 1)[0] + "/") if "/" in path else ""
    same_file: set[str] = set()
    same_dir: set[str] = set()
    token_hit: set[str] = set()
    conf: dict[str, float] = {}

    def collect(rows, bucket: set[str]):
        for r in rows:
            bucket.add(r["id"])
            conf[r["id"]] = float(r["confidence"])

    # 必须按分隔符对齐：只写 LIKE '%a.py' 会把 my_a.py、data.py 也算成同一个文件
    collect(conn.execute(
        "SELECT DISTINCT a.id, a.confidence FROM atoms a JOIN anchors n ON n.atom_id=a.id "
        "WHERE (n.path = ? OR n.path LIKE ? ESCAPE '\\') AND a.status!='retired'",
        (base, f"%/{_like(base)}")).fetchall(), same_file)

    if hint and folder:
        collect(conn.execute(
            "SELECT DISTINCT a.id, a.confidence FROM atoms a JOIN anchors n ON n.atom_id=a.id "
            "WHERE n.path LIKE ? ESCAPE '\\' AND a.status!='retired'",
            (f"{_like(folder)}%",)).fetchall(), same_dir)

    stem = base.rsplit(".", 1)[0]
    for token in [t for t in re.split(r"[^A-Za-z0-9]+", stem)
                  if len(t) >= 5 and t.lower() not in STOP] if hint else []:
        like = f"%{_like(token)}%"
        df = conn.execute(
            "SELECT COUNT(DISTINCT a.id) FROM atoms a LEFT JOIN anchors n ON n.atom_id=a.id "
            "WHERE a.status!='retired' AND (a.claim LIKE ? ESCAPE '\\' OR a.mechanism LIKE ? ESCAPE '\\' "
            "OR a.conditions LIKE ? ESCAPE '\\' OR a.counter LIKE ? ESCAPE '\\' "
            "OR n.path LIKE ? ESCAPE '\\')",
            (like, like, like, like, like)).fetchone()[0]
        if df == 0 or df > 5:
            continue
        collect(conn.execute(
            "SELECT DISTINCT a.id, a.confidence FROM atoms a LEFT JOIN anchors n ON n.atom_id=a.id "
            "WHERE a.status!='retired' AND (a.claim LIKE ? ESCAPE '\\' OR a.mechanism LIKE ? ESCAPE '\\' "
            "OR a.conditions LIKE ? ESCAPE '\\' OR a.counter LIKE ? ESCAPE '\\' "
            "OR n.path LIKE ? ESCAPE '\\')",
            (like, like, like, like, like)).fetchall(), token_hit)

    def score(atom_id: str) -> int:
        # 证据强度：同文件 3 分 > 罕见词干 2 分 > 仅同目录 1 分。
        # 实测：纯目录优先会让同目录但话题无关的条目压过跨文件同主题条目。
        return (3 * (atom_id in same_file) + 2 * (atom_id in token_hit)
                + 1 * (atom_id in same_dir))

    out = []
    for atom_id in sorted(conf, key=lambda i: (-score(i), -conf[i], i)):
        row = db.get_atom(conn, atom_id)
        if row is None:
            continue
        if hint and not re.search(hint, " ".join(
                str(row[k] or "") for k in ("claim", "mechanism", "conditions", "counter")), re.I):
            continue
        out.append(to_dict(conn, row, expand=False))
        if len(out) >= limit:
            break
    return out


def to_dict(conn, row, expand: bool) -> dict:
    item = {k: row[k] for k in row.keys()}
    item["anchors"] = [
        {"kind": a["kind"], "path": a["path"], "commit": a["commit_sha"][:12],
         "excerpt": a["excerpt"], "resolved": a["resolved"]}
        for a in db.anchors_of(conn, row["id"])
    ]
    if expand:
        item["linked"] = _linked(conn, row["id"])
    return item


def _linked(conn, atom_id: str) -> list[dict]:
    """一跳邻居；same-file 边附带共享路径，否则读者看不出这两条为什么相关。"""
    out = []
    for edge in conn.execute(
            "SELECT rel,dst,weight FROM edges WHERE src=? ORDER BY weight DESC, dst LIMIT ?",
            (atom_id, LINKED_LIMIT)):
        other = db.get_atom(conn, edge["dst"])
        if other is None:
            continue
        entry = {"rel": edge["rel"], "id": edge["dst"],
                 "weight": edge["weight"], "claim": other["claim"]}
        if edge["rel"] == "same-file":
            entry["via"] = _shared_paths(conn, atom_id, edge["dst"])
        out.append(entry)
    return out


def _shared_paths(conn, left: str, right: str, limit: int = 2) -> list[str]:
    """列出两条经验共享的代码路径，口径必须与 link 建边时一致（同样排除文档）。"""
    marks = " AND ".join(f"n1.path NOT LIKE '%{s}'" for s in NON_CODE_SUFFIXES)
    rows = conn.execute(
        "SELECT DISTINCT n1.path FROM anchors n1 JOIN anchors n2 ON n1.path = n2.path "
        f"WHERE n1.atom_id=? AND n2.atom_id=? AND n1.path != '' AND {marks} LIMIT ?",
        (left, right, limit))
    return [row["path"] for row in rows]
