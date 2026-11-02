"""把「何时调用经验库」写成可刷新的托管段落。

为什么需要版本标记：AGENTS.md 里的调用规则会随工具描述变化（比如新增
record_dead_end、search 改成分层返回）。没有标记就没有办法判断"这段是不是
旧版"，只能靠人记得；有了标记，`brain init-docs` 只替换块内内容，块外一个字不动。
"""
from __future__ import annotations

import re
from pathlib import Path

RULES_VERSION = 2
START = "<!-- agentbrain-rules: {} -->"
END = "<!-- /agentbrain-rules -->"
_BLOCK = re.compile(r"<!-- agentbrain-rules: (\d+) -->.*?<!-- /agentbrain-rules -->", re.DOTALL)

RULES = """## 经验库（AgentBrain MCP）使用约定

本地有一个跨项目的经验库（MCP 服务名 `agentbrain`，工具
`search / explain / check_diff / record_dead_end / record_outcome`）。
不按下面三条调用，它就等于不存在：

1. **改不熟悉的模块前 → `search`**。2-4 个关键词（模块 + 动作 + 担心的问题），
   并传 `repo` 为当前仓库根目录。零命中即库内没有，按原有流程读代码。
   返回是**索引**（id + 一句话 + score/tier/freshness），需要机制与锚点时再用 `explain`。
   `dead_ends` 里是试过并失败的做法，优先看。
2. **提交或交付前 → `check_diff`**。传完整 `git diff` 原文，不要只给片段。
   `findings` 是探针线索，`dead_ends` 是同一文件上的失败历史，逐条判断并在交付说明里交代。
3. **任务收尾 → `record_outcome`**。按真实结果选信号：`validated`（确实避免了问题）、
   `used`（只提供线索）、`not-used`、`failed`、`corrected`。没有真实判定就不记。
   如果本次尝试失败，用 `record_dead_end` 记下 tried / why_failed / what_worked。
"""


def render() -> str:
    return START.format(RULES_VERSION) + "\n" + RULES.strip() + "\n" + END


def version_of(content: str) -> int:
    match = _BLOCK.search(content)
    return int(match.group(1)) if match else 0


def apply(content: str) -> tuple[str, str]:
    """返回 (新内容, 状态)。状态：missing / current / outdated / appended。"""
    match = _BLOCK.search(content)
    if match:
        if int(match.group(1)) >= RULES_VERSION and match.group(0) == render():
            return content, "current"
        return content[:match.start()] + render() + content[match.end():], "outdated"
    prefix = content.rstrip("\n")
    joined = (prefix + "\n\n" if prefix else "") + render() + "\n"
    return joined, "appended"


def apply_to(repo: str, write: bool = False, filename: str = "AGENTS.md") -> dict:
    root = Path(repo)
    if not root.is_dir():
        return {"error": "仓库目录不存在", "repo": str(root)}
    path = root / filename
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    updated, state = apply(content)
    if write and state != "current":
        path.write_text(updated, encoding="utf-8")
    return {"path": str(path), "state": state, "version": version_of(updated),
            "target_version": RULES_VERSION, "written": bool(write and state != "current"),
            "chars": len(updated)}
