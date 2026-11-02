# AgentBrain

本地经验库（外置大脑）。只存**带证据锚点、可被证伪**的工程经验，不存文档全文、不做语义搜索。

## 它解决什么问题

代码审查里真正值钱的结论往往来自「人读代码 + 回忆踩过的坑」。AgentBrain 不替人做判断，
只做两件机器能做的事：

1. **划定重点**：动手改一块不熟的代码前，按关键词捞出相关经验，附带当时的提交、文件与代码片段。
2. **排除误报**：提交前对 diff 跑属性探针，命中「并发/幂等/超时/缓存/后台任务」这类历史高频坑。

它给不出结论的地方会明说「库内暂无相关经验」，而不是用相似度硬凑。

## 快速开始

```bash
# 1. 登记一个仓库
python -m brain add-source E:\work\my-service

# 2. 从 git 信号挖候选（回滚 / 线上修复 / 修复+补测试 / 反复改动）
python -m brain mine --limit 200

# 3. 候选分流：机检能判的自动定，判不了的留给人工
python -m brain triage            # 只报告
python -m brain triage --apply    # 退休 + 采纳

# 4. 把提交摘要富化成 claim / mechanism / conditions / counter
python -m brain enrich --limit 10
python -m brain enrich --refill   # 只补 conditions 是占位话术的条目

# 5. 从文档里抽经验（锚点 = 原文逐字摘录）
python -m brain doc-ingest docs --limit 5
python -m brain doc-ingest docs --skip-seen   # 只处理没抽过的小节，重跑不重复计费

# 6. 日常使用
python -m brain search "回调 幂等 重复扣款"
python -m brain search "租约" --repo E:\work\my-service   # 多项目库按仓库收窄
git diff | python -m brain check
python -m brain replay            # 回放：探针能否认出自己来源的坏代码
python -m brain eval              # 检索回归：真实查询 + 人工标注的期望条目
python -m brain funnel            # 四层漏斗：知识 → 加工 → 使用 → 反馈
```

MCP 接入：`python -m brain mcp-config` 输出客户端配置片段，工具名
`search / explain / record_atom / check_diff / audit_diff / record_outcome / review_atom / review_pending / stats`。

## 数据模型

| 表 | 作用 |
| --- | --- |
| `sources` | 登记的种子仓库 |
| `atoms` | 一条经验：claim（可证伪陈述）+ mechanism + conditions + counter + 来源 derivation |
| `anchors` | 证据锚点：commit / file + 行号 + 原文片段，`verify` 机检是否仍可解析 |
| `edges` | 只连可机检事实：同提交、同文件；不连文档路径、不连退休条目 |
| `outcomes` | 反馈事件；`triage:` / `audit:` 前缀是机器判定，只有人工与任务反馈才算真实使用 |
| `clues` | guard-only 降级的线索：说明"这里可能有防护逻辑"，本身不是知识 |
| `revisions` | 富化留底（old vs new），支持 `revert` 回退 |

状态机：`candidate → accepted | retired`，没有其它状态。

## 三条设计红线

1. **没有锚点就不是知识**。`record_atom` 缺 commit/path 直接拒绝；`verify` 用
   「这条提交/文件/片段现在还能不能找到」回答可追溯性，不做语义判断。
2. **机器判定 ≠ 真实使用**。`triage:` 采纳只是格式合格，`audit:` 疑似命中只是线索，
   两者都不改置信度；只有任务里来的 `validated / invalidated` 才算反馈。
3. **探针是数据不是代码**。`probes/*.json` 用 `added_any / require_any / unless_any / atom_hint`
   描述属性，新增规则不需要改程序；`brain replay` 检验规则能否认出它自己来源的坏代码。

## 目录

```
brain/       核心模块（db / search / probes / audit / enrich / refine / link / funnel / replay / doc_ingest）
probes/      属性探针 JSON
data/        brain.db（不入版本库）
logs/        mcp-calls.jsonl 与历史轮转文件
tests/       单元测试（python -m unittest discover -s tests -q）
```

## 已知边界

- 检索是关键词 AND + 中文 2-gram 覆盖度，不是向量检索；同义改写召回有限。
- 检索默认只返回「接近最佳命中」的 3 条（MCP）／5 条（CLI），不凑满额；宁缺毋滥，
  零命中就是库内没有。要看完整证据链用 `explain`。
- 只有 git 信号一条自动准入路径，静态导入的 GitHub 项目不会自己长出经验。
- 回放只能验证「规则与证据自洽」，不能验证「这条经验在真实任务里有用」。
- 文档抽取在 API 参考类文档上产出偏项目事实，优先指向运维手册 / 事故复盘 / 设计权衡类文档。
