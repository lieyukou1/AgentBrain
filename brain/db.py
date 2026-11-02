"""数据层：atoms / anchors / edges / outcomes。只依赖标准库 sqlite3。

设计约束：
- 不设 category 字段。语义由锚点与边生长出来。
- 每条 atom 必须至少一个 anchor，claim 必须可证伪（由 verify 与人工 review 保证）。
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

def home() -> Path:
    return Path(os.environ.get("BRAIN_HOME", Path(__file__).resolve().parents[1]))


def db_path() -> Path:
    p = home() / "data" / "brain.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  path TEXT PRIMARY KEY,
  added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS atoms(
  id TEXT PRIMARY KEY,
  claim TEXT NOT NULL,
  mechanism TEXT NOT NULL DEFAULT '',
  conditions TEXT NOT NULL DEFAULT '',
  counter TEXT NOT NULL DEFAULT '',
  derivation TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0.3,
  status TEXT NOT NULL DEFAULT 'candidate',
  repo TEXT NOT NULL DEFAULT '',
  commit_sha TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anchors(
  atom_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL DEFAULT '',
  commit_sha TEXT NOT NULL DEFAULT '',
  line_start INTEGER,
  line_end INTEGER,
  excerpt TEXT NOT NULL DEFAULT '',
  resolved INTEGER,
  PRIMARY KEY(atom_id, kind, path, commit_sha, excerpt)
);
CREATE TABLE IF NOT EXISTS edges(
  src TEXT NOT NULL,
  rel TEXT NOT NULL,
  dst TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1.0,
  PRIMARY KEY(src, rel, dst)
);
CREATE TABLE IF NOT EXISTS outcomes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  atom_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clues(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL,
  path TEXT NOT NULL,
  token TEXT NOT NULL,
  excerpt TEXT NOT NULL DEFAULT '',
  commit_sha TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  UNIQUE(repo, commit_sha, path, token)
);
CREATE TABLE IF NOT EXISTS revisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  atom_id TEXT NOT NULL,
  old_claim TEXT NOT NULL DEFAULT '',
  old_mechanism TEXT NOT NULL DEFAULT '',
  old_counter TEXT NOT NULL DEFAULT '',
  new_claim TEXT NOT NULL DEFAULT '',
  new_mechanism TEXT NOT NULL DEFAULT '',
  new_counter TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  verdict TEXT NOT NULL DEFAULT 'accepted',
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dead_ends(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL DEFAULT '',
  commit_sha TEXT NOT NULL DEFAULT '',
  tried TEXT NOT NULL,
  why_failed TEXT NOT NULL DEFAULT '',
  what_worked TEXT NOT NULL DEFAULT '',
  conditions TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_atoms_status ON atoms(status);
CREATE INDEX IF NOT EXISTS idx_atoms_repo ON atoms(repo);
CREATE INDEX IF NOT EXISTS idx_anchors_path ON anchors(path);
CREATE INDEX IF NOT EXISTS idx_dead_ends_path ON dead_ends(path);
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# 反馈信号 -> 置信度增量。分档而不是只有"有用/没用"：
# 一个只提供了线索的信号（used）不该和"真的避免了事故"（validated）同权，
# 而 user-correction 级别的错误必须比"这次没帮上"扣得更狠。
# code-conflict 是"代码已经和这条经验矛盾"的强负信号，留给 verify/审计写入。
FEEDBACK_DELTAS = {
    "validated": 0.15,
    "insufficient": -0.2,
    "used": 0.05,
    "not-used": -0.05,
    "failed": -0.25,
    "corrected": -0.35,
    "strengthen": 0.2,
    "weaken": -0.2,
    "code-conflict": -0.4,
    # 探针对应经验本身可能没错，只是规则太宽；轻罚，真正处理是给探针降级
    "probe_false_positive": -0.1,
}


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # 状态词表统一为 candidate / accepted / retired；旧库里的 admitted 就地改名（幂等）
    conn.execute("UPDATE atoms SET status='accepted' WHERE status='admitted'")
    # 富化会整字段覆盖，留底必须包含 conditions，否则回退不完整、也无法对比改了什么
    _ensure_column(conn, "revisions", "old_conditions", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "revisions", "new_conditions", "TEXT NOT NULL DEFAULT ''")
    # 取代链与反馈撤销：历史只追加，不删行；被取代的条目靠这一列从检索里消失
    _ensure_column(conn, "atoms", "superseded_by", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "outcomes", "revoked", "INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def new_id() -> str:
    return "a-" + uuid.uuid4().hex[:12]


def add_atom(conn: sqlite3.Connection, atom: dict) -> str:
    aid = atom.get("id") or new_id()
    ts = now()
    conn.execute(
        "INSERT INTO atoms(id,claim,mechanism,conditions,counter,derivation,confidence,status,"
        "repo,commit_sha,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            aid,
            atom["claim"],
            atom.get("mechanism", ""),
            atom.get("conditions", ""),
            atom.get("counter", ""),
            atom.get("derivation", "git-signal"),
            float(atom.get("confidence", 0.3)),
            atom.get("status", "candidate"),
            atom.get("repo", ""),
            atom.get("commit_sha", ""),
            ts,
            ts,
        ),
    )
    conn.commit()
    return aid


def add_anchor(conn: sqlite3.Connection, atom_id: str, kind: str, path: str = "",
               commit_sha: str = "", line_start: int | None = None,
               line_end: int | None = None, excerpt: str = "") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO anchors(atom_id,kind,path,commit_sha,line_start,line_end,excerpt)"
        " VALUES(?,?,?,?,?,?,?)",
        (atom_id, kind, path, commit_sha, line_start, line_end, excerpt[:400]),
    )
    conn.commit()


def add_edge(conn: sqlite3.Connection, src: str, rel: str, dst: str, weight: float = 1.0) -> None:
    conn.execute(
        "INSERT INTO edges(src,rel,dst,weight) VALUES(?,?,?,?) "
        "ON CONFLICT(src,rel,dst) DO UPDATE SET weight=weight+?",
        (src, rel, dst, weight, weight),
    )
    conn.commit()


def exists_anchor(conn: sqlite3.Connection, commit_sha: str, path: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM anchors WHERE commit_sha=? AND path=? LIMIT 1", (commit_sha, path)
    ).fetchone()
    return row is not None


def add_dead_end(conn: sqlite3.Connection, repo: str, path: str, tried: str,
                 why_failed: str = "", what_worked: str = "", conditions: str = "",
                 commit_sha: str = "") -> dict:
    """记一条"试过但失败"的死路，同仓库同文件的同一种尝试只记一次。

    死路不是知识条目：它没有 claim/confidence，不进 accepted 统计，
    只回答"这里别再试什么"。文本同样在落库前脱敏。
    """
    from .redact import clean_text

    tried = clean_text(tried)[0].strip()
    why_failed = clean_text(why_failed)[0].strip()
    what_worked = clean_text(what_worked)[0].strip()
    conditions = clean_text(conditions)[0].strip()
    if not tried:
        return {"error": "tried 不能为空"}
    row = conn.execute(
        "SELECT id FROM dead_ends WHERE repo=? AND path=? AND tried=? LIMIT 1",
        (repo, path, tried),
    ).fetchone()
    if row is not None:
        return {"id": row["id"], "duplicate": True}
    cur = conn.execute(
        "INSERT INTO dead_ends(repo,path,commit_sha,tried,why_failed,what_worked,conditions,created_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (repo, path, commit_sha, tried, why_failed, what_worked, conditions, now()),
    )
    conn.commit()
    return {"id": cur.lastrowid}


def dead_ends_for(conn: sqlite3.Connection, path: str | None = None,
                  repo: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    sql = "SELECT * FROM dead_ends"
    where, args = [], []
    if path:
        where.append("path=?")
        args.append(path)
    if repo:
        where.append("repo=?")
        args.append(repo)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return list(conn.execute(sql, args))


def dead_end_counts(conn: sqlite3.Connection, paths: list[str]) -> dict[str, int]:
    """一次查完多条路径的死路计数，避免 check_diff 对每个文件各查一次。"""
    if not paths:
        return {}
    marks = ",".join("?" for _ in paths)
    rows = conn.execute(
        f"SELECT path, COUNT(*) n FROM dead_ends WHERE path IN ({marks}) GROUP BY path",
        list(paths),
    )
    return {r["path"]: r["n"] for r in rows}


def add_clue(conn: sqlite3.Connection, repo: str, path: str, token: str,
             excerpt: str, commit_sha: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO clues(repo,path,token,excerpt,commit_sha,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (repo, path, token, excerpt[:300], commit_sha, now()),
    )
    conn.commit()


def get_atom(conn: sqlite3.Connection, atom_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM atoms WHERE id=?", (atom_id,)).fetchone()


def save_revision(conn: sqlite3.Connection, atom_id: str, old: dict, new: dict,
                  model: str, verdict: str, reason: str = "") -> None:
    conn.execute(
        "INSERT INTO revisions(atom_id,old_claim,old_mechanism,old_conditions,old_counter,"
        "new_claim,new_mechanism,new_conditions,new_counter,model,verdict,reason,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (atom_id, old.get("claim", ""), old.get("mechanism", ""),
         old.get("conditions", ""), old.get("counter", ""),
         new.get("claim", ""), new.get("mechanism", ""),
         new.get("conditions", ""), new.get("counter", ""),
         model, verdict, reason, now()),
    )
    conn.commit()


def apply_enrichment(conn: sqlite3.Connection, atom_id: str, new: dict) -> None:
    row = get_atom(conn, atom_id)
    conf = max(float(row["confidence"]), 0.45) if row else 0.45
    conn.execute(
        "UPDATE atoms SET claim=?, mechanism=?, conditions=?, counter=?, derivation='llm-enriched',"
        " confidence=?, updated_at=? WHERE id=?",
        (new["claim"], new.get("mechanism", ""), new.get("conditions", ""),
         new.get("counter", ""), conf, now(), atom_id),
    )
    conn.commit()


def anchors_of(conn: sqlite3.Connection, atom_id: str) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM anchors WHERE atom_id=?", (atom_id,)))


def supersede(conn: sqlite3.Connection, old_id: str, new_id: str) -> dict:
    """用一条新结论取代旧结论：旧条退休，但行与历史都保留。

    取代是"这条不再成立"，不是"这条从未存在"：检索默认不再返回它，
    explain 仍能顺着 superseded_by 找到取代者。
    """
    old = get_atom(conn, old_id)
    if old is None:
        return {"error": "被取代的 atom 不存在", "reason": "missing_old"}
    if get_atom(conn, new_id) is None:
        return {"error": "取代者 atom 不存在", "reason": "missing_new"}
    if old_id == new_id:
        return {"error": "不能取代自己", "reason": "self"}
    if old["superseded_by"]:
        return {"error": f"已被 {old['superseded_by']} 取代，不能重复取代",
                "reason": "already_superseded"}
    conn.execute(
        "UPDATE atoms SET status='retired', superseded_by=?, updated_at=? WHERE id=?",
        (new_id, now(), old_id),
    )
    conn.execute(
        "INSERT INTO outcomes(atom_id,outcome,note,created_at) VALUES(?,?,?,?)",
        (old_id, "superseded", f"by {new_id}", now()),
    )
    conn.commit()
    return {"atom_id": old_id, "superseded_by": new_id, "status": "retired"}


def revoke_outcome(conn: sqlite3.Connection, outcome_id: int) -> dict:
    """撤销一次反馈：只把事件标记为 revoked，并按相反方向回滚置信度。

    这是近似回滚而不是重放：置信度是逐次累加出来的，撤销一次失败信号
    不会把当时因置信度过低而退休的条目自动救活——状态变化要人再看一眼。
    """
    row = conn.execute("SELECT * FROM outcomes WHERE id=?", (int(outcome_id),)).fetchone()
    if row is None:
        return {"error": "outcome 不存在", "reason": "missing"}
    if row["revoked"]:
        atom = get_atom(conn, row["atom_id"])
        return {"outcome_id": int(outcome_id), "revoked": True,
                "atom_id": row["atom_id"],
                "confidence": atom["confidence"] if atom else None,
                "note": "已撤销，未重复回滚"}
    conn.execute("UPDATE outcomes SET revoked=1 WHERE id=?", (int(outcome_id),))
    atom = get_atom(conn, row["atom_id"])
    if atom is not None:
        # 按当初的增量反向回滚；未知信号（如 superseded）没有增量，只标记撤销
        conf = float(atom["confidence"]) - FEEDBACK_DELTAS.get(row["outcome"], 0.0)
        conn.execute(
            "UPDATE atoms SET confidence=?, updated_at=? WHERE id=?",
            (round(max(0.0, min(1.0, conf)), 2), now(), row["atom_id"]),
        )
    conn.commit()
    updated = get_atom(conn, row["atom_id"])
    return {"outcome_id": int(outcome_id), "revoked": True,
            "atom_id": row["atom_id"],
            "confidence": updated["confidence"] if updated else None}


def record_outcome(conn: sqlite3.Connection, atom_id: str, outcome: str, note: str = "",
                   adjust: bool = True) -> None:
    """记一次使用反馈；adjust=False 只留事件，不改置信度与状态。

    调用方用 adjust=False 表示「这是线索不是结论」：LLM 报出的命中、
    批量规则判定都属于线索，只有真实任务里的确认才该给置信度加分。
    """
    conn.execute(
        "INSERT INTO outcomes(atom_id,outcome,note,created_at) VALUES(?,?,?,?)",
        (atom_id, outcome, note, now()),
    )
    if not adjust:
        conn.commit()
        return
    # 使用反馈直接驱动置信度，这是 L2 自我优化
    row = get_atom(conn, atom_id)
    if row is not None:
        delta = FEEDBACK_DELTAS.get(outcome, 0.0)
        conf = max(0.0, min(1.0, float(row["confidence"]) + delta))
        status = row["status"]
        # 任何负信号把置信度压到 0.15 以下都退休，而不只是 insufficient
        if delta < 0 and conf < 0.15:
            status = "retired"
        conn.execute(
            "UPDATE atoms SET confidence=?, status=?, updated_at=? WHERE id=?",
            (round(conf, 2), status, now(), atom_id),
        )
    conn.commit()


def _freshness_distribution(conn: sqlite3.Connection) -> dict:
    """按 search.freshness_of 的定义统计各档条目数（只统计未退休条目）。"""
    dist = {"ok": 0, "partial": 0, "unverified": 0, "missing": 0}
    rows = conn.execute(
        "SELECT a.id,"
        " COUNT(n.kind) AS total,"
        " SUM(CASE WHEN n.resolved=1 THEN 1 ELSE 0 END) AS ok_n,"
        " SUM(CASE WHEN n.resolved IS NULL THEN 1 ELSE 0 END) AS null_n"
        " FROM atoms a LEFT JOIN anchors n ON n.atom_id=a.id"
        " WHERE a.status!='retired' GROUP BY a.id"
    )
    for r in rows:
        total, ok_n, null_n = r["total"] or 0, r["ok_n"] or 0, r["null_n"] or 0
        if total == 0 or null_n == total:
            level = "unverified"
        elif ok_n == total:
            level = "ok"
        elif ok_n > 0:
            level = "partial"
        else:
            level = "missing"
        dist[level] += 1
    return dist


def stats(conn: sqlite3.Connection) -> dict:
    def one(sql: str, *args):
        row = conn.execute(sql, args).fetchone()
        return row[0] if row else 0

    total = one("SELECT COUNT(*) FROM atoms")
    by_status = {
        r["status"]: r["n"]
        for r in conn.execute("SELECT status, COUNT(*) n FROM atoms GROUP BY status")
    }
    anchors_total = one("SELECT COUNT(*) FROM anchors")
    anchors_resolved = one("SELECT COUNT(*) FROM anchors WHERE resolved=1")
    anchors_unverified = one("SELECT COUNT(*) FROM anchors WHERE resolved IS NULL")
    return {
        "atoms": total,
        "by_status": by_status,
        # 证据状态分布：检索时 missing 不返回，其余三档按折扣排序。
        # 与 search.freshness_of 同一口径，但用一次 GROUP BY 算完，避免逐条查锚点。
        "freshness": _freshness_distribution(conn),
        "anchors": anchors_total,
        "anchor_resolution_rate": round(anchors_resolved / anchors_total, 3) if anchors_total else 0.0,
        "anchors_unverified": anchors_unverified,
        "edges": one("SELECT COUNT(*) FROM edges"),
        "outcomes": one("SELECT COUNT(*) FROM outcomes"),
        "clues": one("SELECT COUNT(*) FROM clues WHERE status='pending'"),
        "dead_ends": one("SELECT COUNT(*) FROM dead_ends"),
        "sources": [r["path"] for r in conn.execute("SELECT path FROM sources ORDER BY added_at")],
    }
