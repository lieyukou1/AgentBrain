"""verify：机器可判的锚点校验。

只回答一个事实问题：这条知识引用的提交/文件/代码行，现在还能不能在仓库里找到。
不做语义判断（语义判断留给人工两键与使用反馈）。
"""
from __future__ import annotations

from pathlib import Path

from . import db, gitutil


def verify_atom(conn, atom_row) -> tuple[int, int]:
    """返回 (已解决锚点数, 锚点总数)。"""
    anchors = db.anchors_of(conn, atom_row["id"])
    repo = atom_row["repo"]
    resolved = 0
    for a in anchors:
        ok = _check_anchor(repo, dict(a))
        conn.execute(
            "UPDATE anchors SET resolved=? WHERE atom_id=? AND kind=? AND path=? AND commit_sha=? AND excerpt=?",
            (1 if ok else 0, a["atom_id"], a["kind"], a["path"], a["commit_sha"], a["excerpt"]),
        )
        resolved += 1 if ok else 0
    conn.commit()
    return resolved, len(anchors)


def _check_anchor(repo: str, a: dict) -> bool:
    sha = a.get("commit_sha") or ""
    path = a.get("path") or ""
    excerpt = (a.get("excerpt") or "").strip()

    if a["kind"] == "commit" or (sha and not path):
        if not sha:
            return False
        code, _ = gitutil.git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        return code == 0

    if not path:
        return False
    if sha:
        text = gitutil.file_at(repo, sha, path)
    else:
        p = Path(repo) / path
        text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
    if text is None:
        # 该提交正是把这个文件删掉时，"现在查不到"本身就是它要证明的事实。
        # 否则删除类经验（如清理共享资产）永远无法把这条锚点算作已解析。
        return bool(sha) and path in gitutil.changed_files(repo, sha)
    if not excerpt:
        return True
    return excerpt in text


def verify_all(conn, only_candidates: bool = False) -> dict:
    sql = "SELECT * FROM atoms"
    if only_candidates:
        sql += " WHERE status='candidate'"
    rows = list(conn.execute(sql))
    ok_atoms = total_anchors = resolved_anchors = 0
    for row in rows:
        r, t = verify_atom(conn, row)
        total_anchors += t
        resolved_anchors += r
        if t and r == t:
            ok_atoms += 1
    return {
        "atoms_checked": len(rows),
        "atoms_fully_resolved": ok_atoms,
        "anchors": total_anchors,
        "anchors_resolved": resolved_anchors,
        "resolution_rate": round(resolved_anchors / total_anchors, 3) if total_anchors else 0.0,
    }
