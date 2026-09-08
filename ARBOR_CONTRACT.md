# Arbor Contract — ARCHITECTURE.md 加载策略选型

## Metric
- Gate: `uv run --locked python -m pytest tests/test_scope_review.py tests/test_deep_self_review.py -q` 全绿
- `scope_pack_fit_ratio`（maximize）：大变更下 scope review pack 装配成功率
- `agent_locate_accuracy`（maximize）：agent 经加载形态定位 10 个指定章节的步数/准确率

## Baseline
- scope_pack_fit_ratio = 0（当前全文形态：170,660 row_cost > 41,198 remain，FATAL）
- agent_locate_accuracy：INIT 实测

## Ambition
- 装配成功率 0 → 100%；agent 定位准确率不低于全文形态；预算内尽力

## Scope
- mixed

## Hard Constraints
- 不改 tests/ 与评测脚本本身
- B_test 仅合并验证用
- P1：文档语义零丢失（索引化是重定位非省略）
- P3 required fail-closed 语义不得放宽
- 保护路径：BIBLE.md、docs/ARCHITECTURE.md、data/、~/Ouroboros/data

## Budget
- ≤ 6 cycles，单实验 ≤ 48h

## Candidate Priors（coordinator 先验）
1. nav map 全覆盖（atlas+fixed part） — 首选
2. 物理切片（Corpus2Skill） — 受控研究已证伪于单文档，预期第一轮淘汰
3. force-include 白名单化（contracts/ → 显式文件表）
4. 文档分区（常驻/归档层重组）
5. 混合（nav map 全覆盖 + force-include 白名单化 + 文档分区/hot-section）— 终态全链路；#1 小改动先行解锁，#4 与 #1 正交可并行，#3 白名单化是 37/40 装配失败主因的直接修复、必含于终态
6. hot-section（高频节常驻 + nav map 补其余）— nav map 的质量对冲变体，若导航测试显示高频节重复读取成本高于常驻

## 背景（问题根因，供 coordinator 上下文）
- scope review lane：37/40 次 "could not assemble required artifact(s)" 全部是 contracts/+ci.yml force-include 目录前缀太粗，touched required 文件全文渲染抢同一 hard budget（另 2 次模型失败、1 次 prompt overflow）
- scope_review.py:303-319 `_load_canonical_context_docs` 把五治理文档全文拼进 scope prompt fixed part——第二个规模风险（149k ARCH + 59k DEV），canonical docs 在 atlas 竞争外，但 fixed part 独立可炸
- 换 1M reviewer 不是解法：ref 调窗口尺寸不动 required 阶梯，owner 已自测不行（ATLAS_MIXED_ASSEMBLY_REMEDY 建议的"配更大窗口 reviewer"已被证伪，勿当备选）

- owner 约束：文档内容不许删（"确实能放这么多东西"）；候选集须覆盖：①现状全文（baseline）②nav map 化 ③文档分区（纯编辑零代码）④hot-section（高频节常驻+nav map 补其余）
- INIT 阶段必须先建复现夹具：从 ~/Ouroboros/data/memory/consciousness_observations.json 提取 campaign 4518 touched-path 集合（~18-26 个 contracts 文件+ci.yml），做成装配 fixture 对比修复前后 build_scope_review_prompt+atlas 结果与 token 账（170,660 row_cost → ~766 nav map，释放 ~211k）；静态阶段用已实测数字淘汰大半，预算留给存活方案的导航测试
- 导航测试机械化判据：以 nav map 行区间为 ground truth，agent ≤K 次 read_file 命中目标节即成功，计 tokens/轮次