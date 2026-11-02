"""检索：LIKE 多词 AND + 一跳图扩展。

刻意不使用向量库：在几千条规模上，LIKE 足够快，而且不会给出"看起来对但没出处"的结果。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

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
# 评分口径的版本号：权重或折扣一改，历史 top_score 就不可比，日志里必须带上它
# v2：加入锚点新鲜度折扣（ok 1.0 / partial 0.85 / unverified 0.9 / missing 不返回）
SCORE_VERSION = 2

# 相对门槛：只给「接近最佳命中」的条目，宁缺毋滥。
# 0.65 / 1 是在 eval/queries.json 上试出来的拐点：recall@3 保持 1.0、
# 标注为相关的条目保留 85%，同时挡掉跨话题噪声；再往上调只会切掉真相关的条目。
# 不设绝对分数下限：分数随查询词数伸缩（单词语查询天然只有 5-7 分），
# 固定阈值会把合法结果一并挡掉，而它想拦的"勉强沾边"用覆盖度门槛更准。
SCORE_FLOOR_RATIO = 0.65
COVERAGE_SLACK = 1

# 锚点新鲜度对排序的折扣。missing 直接不进结果：一条找不到证据的经验，
# 和一条被证伪的经验一样不该继续占据默认返回位；unverified 只是没跑过
# verify，不代表错，所以轻罚。
FRESHNESS_FACTOR = {"ok": 1.0, "partial": 0.85, "unverified": 0.9, "missing": 0.0}

# 跨仓库检索时，同一技术栈的经验更可能适用；但这只是排序偏好，
# 不能改变门槛（否则会凭空多出命中），所以加分与门槛用的是两个数。
STACK_BONUS = 0.1
STACK_MANIFESTS = (
    ("python", ("pyproject.toml", "requirements.txt", "setup.py", "Pipfile", "poetry.lock")),
    ("node", ("package.json", "tsconfig.json", "yarn.lock", "pnpm-lock.yaml")),
    ("java", ("pom.xml", "build.gradle", "build.gradle.kts")),
    ("go", ("go.mod",)),
    ("rust", ("Cargo.toml",)),
    ("docker", ("Dockerfile", "docker-compose.yml", "compose.yaml")),
)
_STACK_CACHE: dict[str, tuple[str, ...]] = {}


def detect_stack(path) -> tuple[str, ...]:
    """按仓库根目录的 manifest 判断技术栈。

    只读文件名，不解析内容：这一步只为排序提供弱信号，解析 manifest 不值得。
    目录不存在（历史仓库已删除）时返回空，不猜。
    """
    key = _norm_repo(path)
    if not key:
        return ()
    if key in _STACK_CACHE:
        return _STACK_CACHE[key]
    root = Path(str(path))
    stacks: list[str] = []
    if root.is_dir():
        for name, files in STACK_MANIFESTS:
            if any((root / f).exists() for f in files):
                stacks.append(name)
    _STACK_CACHE[key] = tuple(stacks)
    return _STACK_CACHE[key]


def freshness_of(conn, atom_id: str) -> str:
    """锚点还能不能支撑这条结论。

    ok        全部锚点都还能解析
    partial   至少一条还能解析，另有失效或未校验的
    unverified 没有锚点，或从没跑过 verify（resolved 全是 NULL）
    missing   有锚点但一条都解析不了
    """
    states = [r["resolved"] for r in db.anchors_of(conn, atom_id)]
    if not states:
        # 直接写库的旧条目没有锚点：无法校验，不等于已失效，不能静默吞掉。
        return "unverified"
    if all(s == 1 for s in states):
        return "ok"
    if any(s == 1 for s in states):
        return "partial"
    if all(s is None for s in states):
        return "unverified"
    return "missing"


def tier_for(matched: str) -> str:
    """证据距离分层（只描述"离当前上下文多远"，不参与排序）。

    L0 同文件/同提交锚点命中；L1 同仓库；L2 跨仓库或来源未知。
    分层是为了让调用方知道该不该把这条当作本项目的约定，而不是当成同一份代码的证据。
    """
    return {"same_file": "L0", "same_repo": "L1"}.get(matched, "L2")


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
                 repo: str | None = None, expand: bool = False,
                 brief: bool = False, freshness: str | None = None,
                 stack: str | None = None) -> list[dict]:
    """fallback=True 时，无命中会退化为"按置信度取前 N 条"；探针关联必须用 fallback=False，
    否则会把不相关的经验硬塞给用户。

    repo 非空时只在该仓库内检索：多项目库里同一个词（如「幂等」）会同时命中几个项目，
    不区分仓库的话，最有用的那条会被别的项目的条目挤掉。

    expand=False 时不带一跳邻居：每次检索都塞 linked 会让返回体翻倍，
    而绝大多数调用只看得上前三条的 claim；要看邻居走 explain。

    过滤与排序都在内存里做：词数很少、表也不大，一条 SQL 拼 OR 反而既慢又容易漏。

    stack 非空时给同栈条目加 STACK_BONUS（只影响排序，不参与门槛判定）：
    跨仓库检索里，"同样写 Python"比"同样写 Java"更可能适用，但这不足以构成一条证据。
    """
    tokens = tokenize(query)
    target = _norm_repo(repo) if repo else ""
    tier = tier_for("same_repo" if target else "")
    rows: list = []
    if tokens:
        need = min_hits(len(tokens))
        scored = []
        for row in conn.execute("SELECT * FROM atoms WHERE status!='retired' AND superseded_by=''"):
            if target and _norm_repo(row["repo"]) != target:
                continue
            value, covered = score(row, tokens)
            if covered < need:
                continue
            fresh = freshness_of(conn, row["id"])
            if freshness and fresh != freshness:
                continue
            factor = FRESHNESS_FACTOR.get(fresh, 0.0)
            if factor <= 0:
                continue  # 证据已经查不到，不能继续占返回位
            value *= factor
            bonus = STACK_BONUS if stack and stack in detect_stack(row["repo"]) else 0.0
            scored.append((value, bonus, covered, row, fresh))
        # 同分再按置信度、id 排，保证结果稳定可复现；加分只进排序键，不进门槛
        scored.sort(key=lambda x: (-(x[0] + x[1]), -float(x[3]["confidence"]), x[3]["id"]))
        if scored:
            floor = scored[0][0] * SCORE_FLOOR_RATIO
            best_covered = scored[0][2]
            # 相对门槛挡「勉强及格」，覆盖度门槛挡「只中一两个词就跟进来」的跨话题条目
            rows = [(value + bonus, row, fresh) for value, bonus, covered, row, fresh in scored
                    if value >= floor and covered >= best_covered - COVERAGE_SLACK][:int(limit)]
    if not rows and fallback:
        pool = [r for r in conn.execute(
            "SELECT * FROM atoms WHERE status!='retired' AND superseded_by=''"
            " ORDER BY confidence DESC")]
        if target:
            pool = [r for r in pool if _norm_repo(r["repo"]) == target]
        for row in pool:
            fresh = freshness_of(conn, row["id"])
            if fresh == "missing" or (freshness and fresh != freshness):
                continue
            rows.append((0.0, row, fresh))
            if len(rows) >= int(limit):
                break
    out = []
    for value, row, fresh in rows:
        item = to_dict(conn, row, expand=expand, freshness=fresh)
        # score 是本次查询内的相对强度（已含新鲜度折扣），跨查询不可比；tier 描述证据距离
        item["score"] = round(float(value), 2)
        item["tier"] = tier
        if brief:
            # 渐进披露：首屏只给索引，正文与锚点用 explain 取。
            # 一次把 mechanism/counter/anchors 全塞进检索，返回体会翻几倍。
            item = {
                "id": item["id"],
                "claim": str(item["claim"])[:140],
                "confidence": item["confidence"],
                "status": item["status"],
                "repo": item["repo"],
                "derivation": item["derivation"],
                "score": item["score"],
                "tier": item["tier"],
                "freshness": item["freshness"],
            }
        out.append(item)
    return out


def dead_ends(conn, query: str, limit: int = 3) -> list[dict]:
    """在"试过并失败"的记录里找相关死路。

    与经验检索共用分词与覆盖度门槛，但不参与 score 排序门槛：死路本来就少，
    漏掉一条的代价比多给一条高，所以只按命中词数排序。
    """
    tokens = tokenize(query)
    if not tokens:
        return []
    need = min_hits(len(tokens))
    scored = []
    for row in conn.execute("SELECT * FROM dead_ends"):
        covered = 0
        for token in tokens:
            if any(token in str(row[k] or "")
                   for k in ("tried", "why_failed", "what_worked", "conditions", "path")):
                covered += 1
        if covered >= need:
            scored.append((covered, row))
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    out = []
    for covered, row in scored[:int(limit)]:
        item = {k: row[k] for k in row.keys()}
        item["matched"] = covered
        out.append(item)
    return out


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
        fresh = freshness_of(conn, atom_id)
        if fresh == "missing":
            continue
        item = to_dict(conn, row, expand=False, freshness=fresh)
        item["score"] = score(atom_id)
        item["tier"] = tier_for("same_file" if atom_id in same_file else "same_repo")
        out.append(item)
        if len(out) >= limit:
            break
    return out


def to_dict(conn, row, expand: bool, freshness: str | None = None) -> dict:
    item = {k: row[k] for k in row.keys()}
    item["anchors"] = [
        {"kind": a["kind"], "path": a["path"], "commit": a["commit_sha"][:12],
         "excerpt": a["excerpt"], "resolved": a["resolved"]}
        for a in db.anchors_of(conn, row["id"])
    ]
    # 让调用方一眼看出这条结论的证据还站不站得住，而不是只看 score
    item["freshness"] = freshness or freshness_of(conn, row["id"])
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
