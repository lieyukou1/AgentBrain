"""mine：从 git 结构性信号挖掘候选知识。

不预设领域分类，只用与领域无关的结构信号：
- 被回滚 / 线上 hotfix
- 缺陷修复且同时新增测试（说明有可复用断言）
- 新增了"防护性代码"（超时/重试/锁/幂等/事务）
- 文件反复修改（churn）
所有候选都是 candidate，必须经 verify + 人工两键才进入 accepted。
"""
from __future__ import annotations

import re
from collections import Counter

from . import db, gitutil, redact

REVERT = re.compile(r"^\s*revert\b|revert\s+[\"']|回滚|还原", re.I)
HOTFIX = re.compile(r"hotfix|紧急|线上|生产事故|rollback|热修", re.I)
FIX = re.compile(r"\bfix(es|ed)?\b|修复|修正|bug|缺陷", re.I)
PERF = re.compile(r"\bperf\b|性能|优化|slow|超时问题", re.I)

# 噪声提交：格式化、依赖升级、WIP、合并、自动生成。它们不携带"为什么"，
# 进库只会把真正的修复信号稀释掉。
NOISE_SUBJECT = [
    re.compile(r"^fix(?:ed)?\s*typo\b", re.I),
    re.compile(r"^typo\b", re.I),
    re.compile(r"^(?:format|lint|prettier|eslint|style|auto-?format)\b", re.I),
    re.compile(r"^(?:chore|build)?\(?deps?\)?[:/]?\s*(?:bump|update|upgrade)\b", re.I),
    re.compile(r"^bump\s+\S+\s+(?:from|to)\b", re.I),
    re.compile(r"^(?:yarn|npm|pnpm)\s+(?:lock|update)\b", re.I),
    re.compile(r"^update\s+(?:lock|yarn\.lock|package-lock)", re.I),
    re.compile(r"^merge\s+(?:branch|pull\s+request|remote)", re.I),
    re.compile(r"^merge\s+\S+\s+into\s+\S+", re.I),
    re.compile(r"^(?:wip|temp|tmp|checkpoint|backup)\b", re.I),
    re.compile(r"^(?:initial\s+commit|first\s+commit|init)\s*$", re.I),
    re.compile(r"^(?:update|change|fix|edit|modify)\s*$", re.I),
    re.compile(r"^\[(?:ci|cd)\s+skip\]", re.I),
    re.compile(r"^(?:auto-?generated|generated\s+by)\b", re.I),
]

# 只改这些文件时，提交本身没有工程含义
NOISE_FILES = [
    re.compile(r"(?:^|/)(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Gemfile\.lock"
               r"|Cargo\.lock|poetry\.lock|composer\.lock|go\.sum)$", re.I),
    re.compile(r"\.min\.(?:js|css)$", re.I),
    re.compile(r"\.map$", re.I),
    re.compile(r"(?:^|/)(?:dist|build|out)/", re.I),
    re.compile(r"(?:^|/)(?:\.DS_Store|Thumbs\.db)$", re.I),
]


def is_noise(subject: str, files: list[str]) -> bool:
    if any(p.search(subject) for p in NOISE_SUBJECT):
        return True
    return bool(files) and all(any(p.search(f) for p in NOISE_FILES) for f in files)


def commit_kind(subject: str) -> str:
    """粗分提交意图，只用于统计与 revert 分流，不参与知识生成。"""
    text = subject.strip()
    patterns = (
        ("revert", REVERT), ("hotfix", HOTFIX), ("fix", FIX), ("perf", PERF),
        ("docs", re.compile(r"^docs?\b|文档", re.I)),
        ("test", re.compile(r"^tests?\b|^spec\b|测试", re.I)),
        ("refactor", re.compile(r"^refactor\b|^cleanup\b|重构|整理", re.I)),
    )
    for name, pattern in patterns:
        if pattern.search(text):
            return name
    return "other"


GUARD = re.compile(
    r"timeout|deadline|retry|backoff|jitter|lock|mutex|synchronized|atomic|idempot|"
    r"unique|transaction|rollback|超时|重试|退避|锁|幂等|唯一|事务",
    re.I,
)
TEST_FILE = re.compile(r"(^|/)(tests?|spec|__tests__)/|(test|spec)[_-]|_test\.", re.I)
# 反复改动的不一定是问题：lock 文件、文档、生成物天然高频，挖出来只是噪声
CHURN_SKIP = re.compile(
    r"(^|/)(docs?|node_modules|vendor|dist|build|\.github)/|\.(md|mdx|ya?ml|json|lock|snap|txt|csv)$"
    r"|\.(spec|test)\.[a-z0-9]+$", re.I
)
LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')
COMMENT = re.compile(r"^\s*(#|//|/\*|\*|<!--|--)")


def code_guard(line: str) -> str | None:
    """只有代码层面的防护才算信号。

    排除注释行、多行字符串（docstring）与"提示语里的词"：先去掉字符串字面量，
    再看剩余部分是否带防护关键词，且必须还有代码运算符。否则「请稍后重试」
    「幂等记录（TTL 365 天）」这类文案会被误判成防护逻辑。
    """
    s = line.strip()
    if not s or COMMENT.match(s):
        return None
    if s.startswith(('"""', "'''")):
        return None
    stripped = LITERAL.sub('""', s)
    m = GUARD.search(stripped)
    if not m:
        return None
    if not re.search(r"[=(:.\[]", stripped):
        return None
    core = re.sub(r'[\s"\'`(){}\[\],;:.]+', "", stripped)
    if len(core) < 6:
        return None
    return m.group(0)


def _signals(subject: str) -> list[str]:
    out = []
    if REVERT.search(subject):
        out.append("revert")
    if HOTFIX.search(subject):
        out.append("hotfix")
    if FIX.search(subject):
        out.append("fix")
    if PERF.search(subject):
        out.append("perf")
    return out


def _claim_for(subject: str, signals: list[str], test_added: bool) -> tuple[str, str, str]:
    """返回 (claim, mechanism, conditions)。claim 必须能被后续使用反驳。"""
    if "revert" in signals:
        return (f"该做法后被回滚：{subject}", "", "同类改动在演进中被撤回")
    if "hotfix" in signals:
        return (f"线上紧急修复：{subject}", "", "发布后暴露的问题")
    if "fix" in signals and test_added:
        return (f"缺陷修复同时补充了测试：{subject}", "", "该缺陷可被测试复现")
    if "perf" in signals:
        return (f"性能相关改动：{subject}", "", "存在可测量的性能问题")
    return ("", "", "")


def mine_repo(conn, repo: str, limit: int = 200, since: str | None = None,
              shas: list[str] | None = None) -> dict:
    repo = str(repo)
    if not gitutil.is_repo(repo):
        return {"repo": repo, "error": "not-a-git-repo", "created": 0, "scanned": 0}

    if shas:
        commits = [c for c in (gitutil.commit_info(repo, s) for s in shas) if c]
    else:
        commits = gitutil.log_commits(repo, limit=limit, since=since)
    created = skipped = clues_added = 0
    by_kind: Counter[str] = Counter()
    for c in commits:
        subject = c["subject"]
        files = gitutil.changed_files(repo, c["sha"])
        if not files:
            continue
        by_kind[commit_kind(subject)] += 1
        # 噪声提交在读 diff、算信号之前就丢掉：它们没有"为什么"可记
        if is_noise(subject, files):
            skipped += 1
            continue
        signals = _signals(subject)
        if not signals:
            continue

        # 回滚 = 这条做法已被证伪。它不该长成一条 claim（没解释为什么失败），
        # 而应该进 dead_ends，让下次改同一文件时被提醒。
        if "revert" in signals:
            for path in files[:3]:
                db.add_dead_end(conn, repo, path, subject,
                                why_failed="该改动随后被回滚", commit_sha=c["sha"])
            created += 1
            continue

        adds = gitutil.added_lines(repo, c["sha"])
        test_added = any(TEST_FILE.search(f) for f in files)
        # guard 只认代码行：注释与提示语不算
        guard_lines = [(f, ln.strip(), code_guard(ln)) for f, ln in adds]
        guard_lines = [(f, ln, tok) for f, ln, tok in guard_lines if tok][:3]

        # 硬信号才升格为 atom；guard-only 降级为探针线索
        hard = ("hotfix" in signals or "perf" in signals
                or ("fix" in signals and test_added))
        if not hard:
            if guard_lines:
                for f, ln, tok in guard_lines:
                    db.add_clue(conn, repo, f, tok, ln, c["sha"])
                    clues_added += 1
            continue

        if conn.execute(
            "SELECT 1 FROM atoms WHERE repo=? AND commit_sha=? AND status!='retired' LIMIT 1",
            (repo, c["sha"]),
        ).fetchone():
            skipped += 1
            continue

        claim, mechanism, conditions = _claim_for(subject, signals, test_added)
        if not claim:
            continue
        claim = redact.clean_text(claim)[0]
        mechanism = redact.clean_text(mechanism)[0]
        conditions = redact.clean_text(conditions)[0]
        aid = db.add_atom(conn, {
            "claim": claim,
            "mechanism": mechanism,
            "conditions": conditions,
            "derivation": "git-signal",
            "confidence": 0.3,
            "repo": repo,
            "commit_sha": c["sha"],
        })
        # 提交摘要与片段都可能带出环境变量；excerpt 一旦被改写就不可校验，
        # 所以命中密钥时直接丢掉片段，只保留 commit / path 锚点。
        subject = redact.clean_text(subject)[0]
        db.add_anchor(conn, aid, "commit", path="", commit_sha=c["sha"], excerpt=subject)
        for f in files[:4]:
            excerpt = next((ln for pf, ln, _ in guard_lines if pf == f), "").strip()
            if excerpt and redact.has_secret(excerpt):
                excerpt = ""
            db.add_anchor(conn, aid, "file", path=f, commit_sha=c["sha"], excerpt=excerpt)
        created += 1

    churn_created = _mine_churn(conn, repo, commits)
    return {"repo": repo, "scanned": len(commits), "created": created,
            "churn_created": churn_created, "skipped": skipped,
            "clues_added": clues_added, "by_kind": dict(by_kind)}


def _mine_churn(conn, repo: str, commits: list[dict]) -> int:
    counter: Counter[str] = Counter()
    for c in commits:
        for f in gitutil.changed_files(repo, c["sha"]):
            counter[f] += 1
    created = 0
    for path, n in counter.most_common(8):
        if n < 3:
            continue
        if CHURN_SKIP.search(path) or TEST_FILE.search(path):
            continue
        claim = f"{path} 在最近 {len(commits)} 次提交中被修改 {n} 次（反复改动信号）"
        if conn.execute(
            "SELECT 1 FROM atoms WHERE repo=? AND claim=? LIMIT 1", (repo, claim)
        ).fetchone():
            continue
        aid = db.add_atom(conn, {
            "claim": claim,
            "conditions": "同一区域被反复修改，通常意味着结构性问题或反复修复",
            "derivation": "churn",
            "confidence": 0.2,
            "repo": repo,
        })
        db.add_anchor(conn, aid, "file", path=path)
        created += 1
    return created
