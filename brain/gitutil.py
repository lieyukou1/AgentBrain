"""git 调用封装：只读，统一编码与失败处理。"""
from __future__ import annotations

import subprocess
from pathlib import Path


def git(repo: str | Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    cmd = ["git", "-c", "core.quotepath=false", "-C", str(repo), "--no-pager", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"<git-error: {exc}>"
    return p.returncode, p.stdout


def is_repo(path: str | Path) -> bool:
    code, out = git(path, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out.strip() == "true"


def commit_count(path: str | Path) -> int:
    code, out = git(path, "rev-list", "--count", "HEAD")
    return int(out.strip()) if code == 0 and out.strip().isdigit() else 0


def log_commits(path: str | Path, limit: int = 200, since: str | None = None) -> list[dict]:
    args = ["log", "--no-merges", f"-n{limit}", "--date=short",
            "--format=%H%x1f%ad%x1f%s"]
    if since:
        args.append(f"--since={since}")
    code, out = git(path, *args)
    if code != 0:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            commits.append({"sha": parts[0], "date": parts[1], "subject": parts[2]})
    return commits


def commit_info(path: str | Path, sha: str) -> dict | None:
    """按提交号取单条摘要；队列驱动的挖掘只处理被 hook 记下的那几个提交。"""
    code, out = git(path, "log", "-1", "--no-merges", "--date=short",
                    "--format=%H%x1f%ad%x1f%s", sha)
    if code != 0:
        return None
    parts = out.strip().split("\x1f")
    if len(parts) != 3:
        return None
    return {"sha": parts[0], "date": parts[1], "subject": parts[2]}


def changed_files(path: str | Path, sha: str) -> list[str]:
    code, out = git(path, "show", "--name-only", "--format=", sha)
    if code != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def added_lines(path: str | Path, sha: str, max_bytes: int = 400_000) -> list[tuple[str, str]]:
    """返回 [(file, 新增行内容)]；只取 + 行，忽略文件头。"""
    code, out = git(path, "show", "--unified=0", "--format=", sha)
    if code != 0:
        return []
    out = out[:max_bytes]
    result: list[tuple[str, str]] = []
    current = ""
    for ln in out.splitlines():
        if ln.startswith("+++ b/"):
            current = ln[6:]
        elif ln.startswith("+++ "):
            current = ""
        elif ln.startswith("+") and not ln.startswith("+++"):
            result.append((current, ln[1:]))
    return result


def file_at(path: str | Path, sha: str, rel: str) -> str | None:
    code, out = git(path, "show", f"{sha}:{rel}")
    return out if code == 0 else None


def head_files(path: str | Path) -> list[str]:
    code, out = git(path, "ls-files")
    if code != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]
