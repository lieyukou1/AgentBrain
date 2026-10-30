# AGENTS.md — AgentBrain 开发须知

> 开工前先读 `README.md`（定位、命令、数据模型）。本文件只写「改这个库时必须守住什么」。

## 1. 不可破坏的不变量

改动前先确认没有违反下面几条，它们比功能更重要：

1. **没有锚点就不是知识**。任何写入路径（CLI `add`、MCP `record_atom`、`mine`、`doc-ingest`）
   都必须落 commit 或 path 锚点，并立刻做一次机检。
2. **状态机只有三态**：`candidate → accepted | retired`。不要新增 `admitted`、`verified` 之类的中间态。
3. **机器判定不改置信度**。`triage:`（机检采纳）与 `audit:`（LLM 疑似命中）只能记事件，不能当真实反馈；
   真实反馈只来自任务结束后的 `validated / invalidated`。
4. **探针是数据**。属性规则写在 `probes/*.json` 的 `added_any / require_any / unless_any` 里，
   不要把人写的规则写成 `probes.py` 里的 if 分支。
5. **锚点校验只回答事实问题**。`verify` 只判「提交/文件/片段还能不能找到」，
   不做语义判断——语义留给人工两键与使用反馈。

## 2. 改动后的验证顺序

```bash
python -m unittest discover -s tests -q     # 全量单测，必须是绿的
python -m brain replay                      # 探针自洽性：有 from_atom 的规则要能认出自己的坏代码
python -m brain check --diff logs/diffs/real-gap.diff   # 基线 diff 不应新增误报
python -m brain funnel                      # 看一眼四层指标有没有异常跳变
```

- 改了探针：必须跑 `replay`。命中不了自己来源的坏代码，要么规则太窄，要么这条经验不是代码形状类
  （后者应摘掉 `from_atom`，不要硬凑规则）。
- 改了文档抽取：重跑一律加 `--skip-seen`，否则每个小节都会重新调用一次模型（去重发生在拿到结果之后，
  默认拦不住这笔开销）。
- 改了检索：用真实查询回归（中文长句、英文标识符、文件名带下划线三种），不要只看单测。
- 改了写入路径：确认 `verify --candidates-only` 的解析率没有下降。

## 3. 代码约定

- **零第三方依赖**：只用 Python 标准库；SQLite 走 `sqlite3`，HTTP 走 `urllib`。
- **迁移必须幂等**：`db.connect()` 里只有 `CREATE TABLE IF NOT EXISTS`、`_ensure_column`、
  以及可重复执行的 `UPDATE`；不要在连接时做一次性的数据搬运。
- **注释写「为什么」**：这个库里每个模块的 docstring 都在解释取舍（为什么不用向量库、
  为什么回放整份文件、为什么 unless 收窄），改逻辑时同步改理由，不要留下与实现矛盾的注释。
- **失败要留痕但不要阻断**：日志写入、链接重建、单条富化失败都不能让主流程崩；
  反过来，锚点文件被删除属于正常事实，不能当异常处理。
- 新增 CLI 子命令：在 `cli.py` 的 `build_parser()` 注册 + 在 `tests/test_brain.py` 的 `CliTests` 加用例。
- 新增 MCP 工具：工具描述必须以「何时用」开头（客户端只露描述给模型），
  并在 `_result_brief()` 里补可聚合的计数，否则 `funnel` 的命中率永远算不出来。

## 4. 环境与密钥

- `BRAIN_HOME` 决定 `data/`、`logs/`、`secrets/` 的位置；测试通过临时目录覆盖它，不要写死路径。
- DeepSeek 密钥优先取 `DEEPSEEK_API_KEY`，其次 `<BRAIN_HOME>/secrets/deepseek.key`；
  任何日志、异常信息、数据库字段都不许出现密钥。
- 单测不许打真实网络：LLM 一律通过 `client=` 注入假客户端。

## 5. 已知坑

- Windows 上 `os.replace` 可能因为文件被占用失败：轮转逻辑必须容忍并继续追加。
- PowerShell 会吞掉 `python -c "..."` 里的双引号，复杂脚本写成临时文件再执行。
- 富化会整字段覆盖，`revisions` 是唯一留底；新增会被覆盖的字段时，必须同时给
  `revisions` 加对应的 `old_* / new_*` 列（幂等迁移）。
- 文档抽取的 `excerpt` 必须逐字可查；任何"顺手修正原文措辞"的想法都会让锚点失效。
