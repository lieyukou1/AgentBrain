"""mine-pr：从 PR / MR 评论里找「被后续提交真正改掉」的意见。

为什么只取这一种配对：评论本身只是个人观点，只有"评论指出的文件随后被改了"
才说明这条意见被采纳并有代码证据。其余评论（点赞、讨论、被忽略的）一律不收，
否则库会变成聊天记录。

约束：
- 全程只读（只有 GET），不写回 GitHub；
- 不落任何 token，也不落评论全文；claim 只保留截断后的一句话；
- 产出仍是 candidate，必须经 verify 与人工两键才进入 accepted。

零第三方依赖：HTTP 用 urllib，鉴权只读 GITHUB_TOKEN。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from . import db, gitutil, redact

API = "https://api.github.com"
# 只处理最近更新的若干 PR，避免一次扫描把 API 配额打光
DEFAULT_PRS = 5
MAX_COMMITS = 20
MAX_COMMENTS = 20
CLAIM_CHARS = 240

REMOTE = re.compile(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?/?$", re.I)
# 代码建议块只留说明文字：直接塞 ``` 会让 claim 变成一段代码
FENCE = re.compile(r"```.*?```", re.S)


def repo_slug(repo) -> str | None:
    code, out = gitutil.git(repo, "remote", "get-url", "origin")
    if code != 0:
        return None
    m = REMOTE.search(out.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _condense(body: str) -> str:
    text = FENCE.sub(" ", body or "")
    text = " ".join(text.split())
    return text[:CLAIM_CHARS]


def _http_get(url: str, token: str) -> list | dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agentbrain-mine-pr",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _pairs(pr: dict, comments: list[dict], commits: list[dict],
           files_for) -> list[dict]:
    """评论 + 它之后修改了同一文件的提交 = 一条可沉淀的候选。"""
    ordered = sorted(commits, key=lambda c: c.get("_date", ""))
    out = []
    for comment in comments[:MAX_COMMENTS]:
        path = comment.get("path") or ""
        if not path or not _condense(comment.get("body", "")):
            continue
        created = str(comment.get("created_at") or "")
        for commit in ordered:
            if commit.get("_date") and created and commit["_date"] <= created:
                continue
            if path not in files_for(commit["sha"]):
                continue
            out.append({"pr": pr.get("number"), "comment_id": comment.get("id"),
                        "path": path, "commit": commit["sha"],
                        "claim": _condense(comment.get("body", ""))})
            break
    return out


def mine(conn, repo: str, limit: int = DEFAULT_PRS, dry_run: bool = False,
         token: str | None = None, fetch=None) -> dict:
    repo = str(repo)
    token = token or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        return {"repo": repo, "error": "缺少 GITHUB_TOKEN（只读权限即可）"}
    slug = repo_slug(repo)
    if not slug:
        return {"repo": repo, "error": "不是 GitHub 仓库（origin 里找不到 owner/repo）"}

    get = fetch or (lambda url: _http_get(url, token))
    try:
        pulls = get(f"{API}/repos/{slug}/pulls?state=all&sort=updated&direction=desc"
                    f"&per_page={int(limit)}")
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"repo": repo, "error": f"拉取 PR 失败：{exc}"}
    if not isinstance(pulls, list):
        return {"repo": repo, "error": "PR 列表格式异常"}

    created = skipped = 0
    pairs: list[dict] = []
    for pr in pulls[:int(limit)]:
        number = pr.get("number")
        try:
            comments = get(f"{API}/repos/{slug}/pulls/{number}/comments?per_page=100")
            commits = get(f"{API}/repos/{slug}/pulls/{number}/commits"
                          f"?per_page={MAX_COMMITS}")
        except (urllib.error.URLError, json.JSONDecodeError):
            skipped += 1
            continue
        if not isinstance(comments, list) or not isinstance(commits, list):
            skipped += 1
            continue
        cache: dict[str, set[str]] = {}

        def files_for(sha: str) -> set[str]:
            if sha not in cache:
                try:
                    detail = get(f"{API}/repos/{slug}/commits/{sha}")
                    cache[sha] = {f.get("filename") for f in detail.get("files") or []}
                except (urllib.error.URLError, json.JSONDecodeError, AttributeError):
                    cache[sha] = set()
            return cache[sha]

        normalized = []
        for c in commits:
            meta = c.get("commit") or {}
            normalized.append({"sha": c.get("sha", ""),
                               "_date": ((meta.get("author") or {}).get("date")
                                         or (meta.get("committer") or {}).get("date") or "")})
        for pair in _pairs(pr, comments, normalized, files_for):
            claim = redact.clean_text(pair["claim"])[0]
            if not claim:
                skipped += 1
                continue
            sha = pair["commit"]
            if conn.execute(
                "SELECT 1 FROM atoms WHERE repo=? AND commit_sha=? AND claim=? LIMIT 1",
                (repo, sha, claim),
            ).fetchone():
                skipped += 1
                continue
            if dry_run:
                pairs.append({**pair, "atom": "", "dry_run": True})
                continue
            aid = db.add_atom(conn, {
                "claim": claim,
                "mechanism": "",
                "conditions": "PR 评论指出后，同一文件在后续提交中被修改",
                "derivation": "pr-comment",
                "confidence": 0.25,
                "repo": repo,
                "commit_sha": sha,
            })
            # 只留 commit 与文件锚点：评论正文不进库，避免把聊天记录当证据
            db.add_anchor(conn, aid, "commit", path="", commit_sha=sha)
            db.add_anchor(conn, aid, "file", path=pair["path"], commit_sha=sha)
            pairs.append({**pair, "atom": aid})
            created += 1

    return {"repo": repo, "slug": slug, "prs": len(pulls[:int(limit)]),
            "created": 0 if dry_run else created,
            "dry_run": dry_run, "skipped": skipped,
            "pairs": pairs[:20]}
