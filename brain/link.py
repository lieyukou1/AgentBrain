"""连边：用共享证据把经验连起来，让 search 的 linked 不再是空数组。

只认可机检的事实，不做语义相似：
  same-commit  两条经验引用同一个提交（权重恒为 1）
  same-file    同一仓库内两条经验引用了同一个代码文件（权重 = 共享文件数）
边双向写入，因为查询只按 src 取。跨仓库不算：两个仓库都有 __init__.py 不代表任何关系。

刻意排除两种噪声：
  - 已退休的条目（退役知识不该继续以"相关"的身份出现）
  - 文档/配置路径（几十条经验都锚在同一份 CONFIGURATION.md 上，那是引用集中，不是代码相关性）
"""
from __future__ import annotations

from . import db

REL_COMMIT = "same-commit"
REL_FILE = "same-file"
# 文档与配置不算代码位置：几十条经验锚在同一份 CONFIGURATION.md 上，那是引用集中
NON_CODE_SUFFIXES = (".md", ".mdx", ".txt", ".json", ".yaml", ".yml")

ACTIVE = "a1.status != 'retired' AND a2.status != 'retired'"
NOT_DOC = " AND ".join(f"n1.path NOT LIKE '%{suffix}'" for suffix in NON_CODE_SUFFIXES)

COMMIT_SQL = f"""
SELECT n1.atom_id AS src, n2.atom_id AS dst, COUNT(DISTINCT n1.commit_sha) AS w
FROM anchors n1
JOIN anchors n2 ON n1.commit_sha = n2.commit_sha AND n1.commit_sha != ''
               AND n1.atom_id < n2.atom_id
JOIN atoms a1 ON a1.id = n1.atom_id
JOIN atoms a2 ON a2.id = n2.atom_id
WHERE a1.repo = a2.repo AND {ACTIVE}
GROUP BY n1.atom_id, n2.atom_id
"""

FILE_SQL = f"""
SELECT n1.atom_id AS src, n2.atom_id AS dst, COUNT(DISTINCT n1.path) AS w
FROM anchors n1
JOIN anchors n2 ON n1.path = n2.path AND n1.path != ''
               AND n1.atom_id < n2.atom_id
JOIN atoms a1 ON a1.id = n1.atom_id
JOIN atoms a2 ON a2.id = n2.atom_id
WHERE a1.repo = a2.repo AND {ACTIVE}
  AND {NOT_DOC}
GROUP BY n1.atom_id, n2.atom_id
"""


def rebuild(conn) -> dict:
    """整表重建这两类边；先删后建保证可反复执行而权重不累加。"""
    conn.execute("DELETE FROM edges WHERE rel IN (?, ?)", (REL_COMMIT, REL_FILE))
    commit_pairs = _insert_pairs(conn, COMMIT_SQL, REL_COMMIT)
    file_pairs = _insert_pairs(conn, FILE_SQL, REL_FILE)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    return {"commit_pairs": commit_pairs, "file_pairs": file_pairs, "edges": total}


def _insert_pairs(conn, sql: str, rel: str) -> int:
    rows = list(conn.execute(sql))
    values = []
    for row in rows:
        weight = float(row["w"])
        values.append((row["src"], rel, row["dst"], weight))
        values.append((row["dst"], rel, row["src"], weight))
    conn.executemany(
        "INSERT INTO edges(src,rel,dst,weight) VALUES(?,?,?,?) "
        "ON CONFLICT(src,rel,dst) DO UPDATE SET weight=excluded.weight",
        values,
    )
    return len(rows)
