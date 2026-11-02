"""git hook：只做轻量入队，不调模型、不阻塞提交。

为什么不让 hook 直接跑 mine：挖掘要读 diff、调 git、可能还要 LLM，
放在 commit 路径上会把一次提交从几十毫秒拖到几秒。这里只追加一行
（时间、触发点、提交号、仓库），真正的处理留给 `brain mine --from-queue`。

hook 是显式安装的（`brain hook install`），不会在 add-source 时偷偷写进
别人的仓库；已有 post-commit/post-merge 会备份成 `.pre-brain` 并继续调用。
"""
from __future__ import annotations

from pathlib import Path

from . import db, gitutil

MARKER = "# agentbrain-hook: 1"
HOOKS = ("post-commit", "post-merge")
PRE_COMMIT = "pre-commit"
REPLAY_MIN_HIT_RATE = 0.9


def queue_path() -> Path:
    path = db.home() / "data" / "hook-queue.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _hooks_dir(repo: Path) -> Path:
    code, out = gitutil.git(repo, "rev-parse", "--git-path", "hooks")
    if code != 0 or not out.strip():
        raise OSError("不是 git 仓库")
    path = Path(out.strip())
    return path if path.is_absolute() else (repo / path).resolve()


def _script(repo: Path, trigger: str) -> str:
    """POSIX sh：Git for Windows 用 Git Bash 执行 hook，不依赖 PowerShell。"""
    return (
        "#!/bin/sh\n"
        f"{MARKER}\n"
        f'backup="$(dirname "$0")/{trigger}.pre-brain"\n'
        '[ -x "$backup" ] && "$backup" "$@"\n'
        f"printf '%s\\t%s\\t%s\\t%s\\n' \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\" \"{trigger}\" "
        f"\"$(git -C '{repo.as_posix()}' rev-parse HEAD 2>/dev/null)\" "
        f"'{repo.as_posix()}' >> '{queue_path().as_posix()}'\n"
        "exit 0\n"
    )


def _pre_commit_script() -> str:
    """提交前门禁：回放不自洽就拒绝提交。

    与 post-* 不同，这条会阻塞提交，所以只在显式 `--pre-commit` 时安装。
    PYTHONPATH 指向知识库安装目录，否则在别的仓库里 `python -m brain` 找不到包。
    """
    root = db.home().as_posix()
    return (
        "#!/bin/sh\n"
        f"{MARKER}\n"
        'backup="$(dirname "$0")/pre-commit.pre-brain"\n'
        '[ -x "$backup" ] && "$backup" "$@" || exit $?\n'
        f"PYTHONPATH='{root}' BRAIN_HOME='{root}' "
        f"python -m brain replay --min-hit-rate {REPLAY_MIN_HIT_RATE} >&2\n"
    )


def install(repo: str, pre_commit: bool = False) -> dict:
    repo_path = Path(repo).resolve()
    if not gitutil.is_repo(str(repo_path)):
        return {"error": "不是 git 仓库", "repo": str(repo_path)}
    hooks = _hooks_dir(repo_path)
    hooks.mkdir(parents=True, exist_ok=True)
    installed = []
    triggers = list(HOOKS) + ([PRE_COMMIT] if pre_commit else [])
    for trigger in triggers:
        target = hooks / trigger
        existing = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
        if MARKER in existing:
            installed.append(trigger)
            continue  # 已是我们的版本，重装不能把备份覆盖成自己
        if existing and not (hooks / f"{trigger}.pre-brain").exists():
            (hooks / f"{trigger}.pre-brain").write_text(existing, encoding="utf-8")
            (hooks / f"{trigger}.pre-brain").chmod(0o755)
        script = _pre_commit_script() if trigger == PRE_COMMIT else _script(repo_path, trigger)
        target.write_text(script, encoding="utf-8")
        target.chmod(0o755)
        installed.append(trigger)
    return {"repo": str(repo_path), "installed": installed, "queue": str(queue_path())}


def uninstall(repo: str) -> dict:
    repo_path = Path(repo).resolve()
    try:
        hooks = _hooks_dir(repo_path)
    except OSError as exc:
        return {"error": str(exc), "repo": str(repo_path)}
    removed, restored = [], []
    for trigger in (*HOOKS, PRE_COMMIT):
        target = hooks / trigger
        if not target.exists():
            continue
        content = target.read_text(encoding="utf-8", errors="replace")
        if MARKER not in content:
            continue  # 不是我们的 hook，不碰
        target.unlink()
        removed.append(trigger)
        backup = hooks / f"{trigger}.pre-brain"
        if backup.exists():
            backup.replace(target)
            restored.append(trigger)
    return {"repo": str(repo_path), "removed": removed, "restored": restored}


def read_queue() -> list[dict]:
    """读取入队记录；重复行按 (repo, sha, trigger) 去重，坏行跳过。"""
    path = queue_path()
    if not path.exists():
        return []
    seen: set[tuple[str, str, str]] = set()
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        ts, trigger, sha, repo = parts
        key = (repo, sha, trigger)
        if not sha or key in seen:
            continue
        seen.add(key)
        out.append({"ts": ts, "trigger": trigger, "sha": sha, "repo": repo})
    return out


def consume(entries: list[dict]) -> int:
    """移除已处理的条目，保留处理失败的；返回移除数量。"""
    path = queue_path()
    if not path.exists():
        return 0
    done = {(e["repo"], e["sha"], e["trigger"]) for e in entries}
    kept = []
    removed = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and (parts[3], parts[2], parts[1]) in done:
            removed += 1
            continue
        kept.append(line)
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return removed
