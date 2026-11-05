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
python -m brain mine-pr --repo E:\work\my-service --limit 5 --dry-run  # GitHub PR 评论（只读，需 GITHUB_TOKEN）

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
python -m brain search "租约" --freshness ok              # 只看证据仍可解析的条目
python -m brain search "租约" --stack python              # 跨仓库检索时同栈优先；省略则按 repo/cwd 自动识别
git diff | python -m brain check
python -m brain replay            # 回放：探针能否认出自己来源的坏代码
python -m brain replay --min-hit-rate 0.9   # 门禁：低于阈值时退出码 2（CI / pre-commit 用）
python -m brain eval              # 检索回归：真实查询 + 人工标注的期望条目
python -m brain funnel            # 四层漏斗：知识 → 加工 → 使用 → 反馈（含 token 成本与探针误报比）
python -m brain brief             # 有界快照：高置信结论 / 待验证 / 最近变更 / 死路

# 7. 自动采集（可选）：hook 只入队，不调模型、不阻塞提交
python -m brain hook --repo E:\work\my-service
python -m brain hook --repo E:\work\my-service --pre-commit   # 额外加提交前回放门禁
python -m brain mine --from-queue
python -m brain add --claim "..." --path a.py --supersedes <旧 atom id>
python -m brain revoke <outcome_id>   # 撤销一次误记的反馈

# 8. 失败尝试与指令托管
python -m brain dead-end --path a.py --tried "改成批量提交" \
    --why "下游只能处理单条" --worked "保持逐条 + 幂等键"   # 记录一条死路
python -m brain dead-ends --path a.py                      # 查这个文件试过什么
python -m brain init-docs --repo E:\work\my-service --write # 把使用规则写进 AGENTS.md 托管段
```

MCP 接入：`python -m brain mcp-config` 输出客户端配置片段，工具名
`search / explain / record_atom / record_dead_end / check_diff / audit_diff / record_outcome /
revoke_outcome / review_atom / review_pending / stats`。

## 数据模型

| 表 | 作用 |
| --- | --- |
| `sources` | 登记的种子仓库 |
| `atoms` | 一条经验：claim（可证伪陈述）+ mechanism + conditions + counter + 来源 derivation |
| `anchors` | 证据锚点：commit / file + 行号 + 原文片段，`verify` 机检是否仍可解析 |
| `edges` | 只连可机检事实：同提交、同文件；不连文档路径、不连退休条目 |
| `outcomes` | 反馈事件；`triage:` / `audit:` 前缀是机器判定，只有人工与任务反馈才算真实使用 |
| `clues` | guard-only 降级的线索：说明"这里可能有防护逻辑"，本身不是知识 |
| `dead_ends` | 试过并失败的路径：tried / why_failed / what_worked，检索时随经验一起返回 |
| `revisions` | 富化留底（old vs new），支持 `revert` 回退 |

状态机：`candidate → accepted | retired`，没有其它状态。

## 五条设计红线

1. **没有锚点就不是知识**。`record_atom` 缺 commit/path 直接拒绝；`verify` 用
   「这条提交/文件/片段现在还能不能找到」回答可追溯性，不做语义判断。
2. **机器判定 ≠ 真实使用**。`triage:` 采纳只是格式合格，`audit:` 疑似命中只是线索，
   两者都不改置信度；只有任务里来的 `validated / invalidated` 才算反馈。
3. **探针是数据不是代码**。属性规则写在 `probes/*.json` 的 `added_any / require_any / unless_any`
   里，新增规则不需要改程序；`brain replay` 检验规则能否认出它自己来源的坏代码。
4. **密钥不进库**。四条写入路径（`add` / `record_atom` / `mine` / `doc-ingest`）落库前统一脱敏；
   锚点 excerpt 命中密钥时直接丢弃片段，而不是改写原文——改过的"原文"无法再校验。
5. **结论变了要取代，不要改写**。`supersedes` 让旧条退休并留下指针，检索默认不再返回它，
   `explain` 仍能顺着链看到新旧关系；反馈同理，记错了用 `revoke` 撤销，不删事件。

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
- 锚点全部失效、或已被 `supersedes` 取代的条目默认不返回；前者仍能用 `explain` 看到，
  后者顺着 `superseded_by` 找到取代者。
- 只有 git 信号一条自动准入路径，静态导入的 GitHub 项目不会自己长出经验。
- `mine-pr` 只支持 GitHub，且只收「评论指出后同一文件被后续提交修改」的配对；
  需要只读 `GITHUB_TOKEN`，评论正文只保留截断后的一句话，不落全文。
- 探针误报靠 `record_outcome(..., "probe_false_positive", note="probe:<id>")` 累积；
  命中 ≥3 次且误报率 ≥50% 时，`check_diff` 自动把它降级为 clue（仍留痕，不再告警）。
- 回放只能验证「规则与证据自洽」，不能验证「这条经验在真实任务里有用」。
- 文档抽取在 API 参考类文档上产出偏项目事实，优先指向运维手册 / 事故复盘 / 设计权衡类文档。
