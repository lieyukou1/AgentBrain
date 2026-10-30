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
CREATE INDEX IF NOT EXISTS idx_atoms_status ON atoms(status);
CREATE INDEX IF NOT EXISTS idx_atoms_repo ON atoms(repo);
CREATE INDEX IF NOT EXISTS idx_anchors_path ON anchors(path);
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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
        conf = float(row["confidence"])
        if outcome == "validated":
            conf = min(1.0, conf + 0.15)
        elif outcome == "insufficient":
            conf = max(0.0, conf - 0.2)
        status = row["status"]
        if outcome == "insufficient" and conf < 0.15:
            status = "retired"
        conn.execute(
            "UPDATE atoms SET confidence=?, status=?, updated_at=? WHERE id=?",
            (round(conf, 2), status, now(), atom_id),
        )
    conn.commit()


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
        "anchors": anchors_total,
        "anchor_resolution_rate": round(anchors_resolved / anchors_total, 3) if anchors_total else 0.0,
        "anchors_unverified": anchors_unverified,
        "edges": one("SELECT COUNT(*) FROM edges"),
        "outcomes": one("SELECT COUNT(*) FROM outcomes"),
        "clues": one("SELECT COUNT(*) FROM clues WHERE status='pending'"),
        "sources": [r["path"] for r in conn.execute("SELECT path FROM sources ORDER BY added_at")],
    }
