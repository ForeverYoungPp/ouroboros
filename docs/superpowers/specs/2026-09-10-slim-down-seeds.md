# Ouroboros 精简重构 — Seed 包（供 `ooo auto` / `generate_seed` 消费）

- 日期：2026-09-10
- 分支：`design/slim-down-engram`
- 主规格：`docs/superpowers/specs/2026-09-10-ouroboros-slim-down-design.md`
- 角色规格：`docs/superpowers/specs/2026-09-10-agent-roles-design.md`

## 0. 为什么需要这份文档

`ouroboros_generate_seed` 的 `session_context` 要求 **`goal`** 与 **`acceptance_criteria`**（required，**verifiable checks**），外加 `constraints` / `decisions` / `project_type`。主规格里有 goal、constraints、decisions，**但没有 AC**——§11 是散文式验证策略（其中 19 处命令形片段是原料，不是可判定的验收条件）。

且 `ooo auto` 一次吃**一个** goal，而主规格覆盖 **5 个互相独立的子系统**。

本文补齐这两件：**子项目划分** + **每份 Seed 的 AC**。

## 1. 子项目划分（5 个，各成一份 Seed）

| Seed | 范围 | 依赖 | 现在能否开跑 |
|---|---|---|---|
| **0** | 切 import 图（主规格 §9.2） | 无 | ✅ **阻塞项为零** |
| **1** | 记忆外接 Engram（§5，含 §5.15 记忆模型） | 0 | ❌ 1 项未决 |
| **2** | 删计费投影（§6） | 0 | ❌ 4 项未决 |
| **3** | Claudexor → omp（§7） | 0 | ❌ 1 项待实测 |
| **4** | 面轴收敛（角色规格 §4-§9） | 1、2 | ❌ 需先做 150 处分类 |

**为什么必须拆**：`ooo` 的 A-grade 门要求 AC 可穷举可判定；「把三件事都做完」这种 goal 的 AC 无法穷举，且执行会无界。writing-plans 的 Scope Check 也明说多子系统必须各成 plan，每份自己产出可运行可测的软件。

---

## 2. Seed 0 —— 切 import 图（**可直接开跑**）

### goal

在主规格 §9.1 删除任何模块之前，切断「保留下来的模块」对「将被删除的模块」的全部 import 依赖，使 server 与 supervisor 在被删模块缺席时仍能正常启动。**此阶段不删除任何文件。**

### acceptance_criteria

> **权威版在 `docs/superpowers/specs/seed-0-import-graph.yaml`（`seed_934e30c1d249`，10 条 AC）。** 下面是同一内容的可读摘要；形态已按 §6.1 第 2 条的要求写成「命令 + 逐字期望输出/退出码」。

**两个模块集合（这是原稿最大的缺口——它从未列出模块，只各探一个，执行器可能漏一批而 AC 全绿）：**

| 集合 | 成员 | 用途 |
|---|---|---|
| **探针集**（将被【整体】删除，12 个；本阶段只切依赖不删文件） | `memory.py`、`consolidator.py`、`tools/memory_tools.py`、`cost_projection.py`、`_usage_response.py`、`_usage_rows.py`、`_usage_rows_memo.py`、`claudexor_daemon.py`、`claudexor_runtime.py`、`gateways/claudexor.py`、`gateway/claudexor_accounts.py`、`gateway/claudexor_quota.py` | AC-0.1/0.2/0.3 的**逐个**探测对象 |
| **保护集**（部分保留或全部保留，**不得当整删**，也不是探针目标） | `semantic_dedup.py`（全保留，P2/P3 免疫队列去重器）、`usage_accounting.py` 与 `usage_ledger.py`（保留托管与尝试状态机）、`pricing.py`（保留 `infer_*`）、`reflection.py`（保留 `should_generate_reflection`/结构化候选/`_update_patterns`）、`tools/knowledge.py`（保留 patterns/backlog 路径与三个跨模块符号） | AC-0.10 的**仍可 import** 断言对象 |

**AC（10 条）**：

- **AC-0.1** 探针集 **12 个逐个**跑 server 导入探针——用一条 bash 循环把它们依次 `mv` 走、跑 `import server`、`mv` 回。期望：输出 **12 行**，每行以 `EXIT=0` 结尾。**行数少于 12 即失败，任一行非 0 即失败。**
- **AC-0.2** 同上 12 个，探针换成 `import supervisor.events`。期望 12 行全 `EXIT=0`。
- **AC-0.3** 同上 12 个，探针换成 `import ouroboros.gateway.extensions`。期望 12 行全 `EXIT=0`。
- **AC-0.4** `pytest tests/test_gateway_parity.py -q` 期望退出码 0 且含 `passed`。
- **AC-0.5** `pytest tests/test_server_control_panic_daemon.py -q` 期望退出码 0；附加静态检查——`grep -n -A2 claudexor_daemon ouroboros/server_control.py` 的输出中不得存在「import 位于 `except Exception: pass`（或仅 log）的 try 块内」。
- **AC-0.6** `pytest web/tests -q` 期望退出码 0；`claudexor_status_store.js` 的 7 个 importer 仍可解析。
- **AC-0.7** `git diff --stat --diff-filter=D` 期望**输出为空**（零删除）。
- **AC-0.8** 三条逐字契约（HTTP 503 body 形状 / 异常类型与定义位置 / 工具缺席时的返回）——见 seed 文件与 §6.2。
- **AC-0.9** `collect_routes()` 在改动前后输出**同一个整数**。
- **AC-0.10** 保护集 6 个模块**仍可 import**：一条 `python -c "import ouroboros.semantic_dedup, ouroboros.usage_accounting, ouroboros.usage_ledger, ouroboros.pricing, ouroboros.reflection, ouroboros.tools.knowledge; print('OK')"` 期望输出 `OK` 且退出码 0。
- **AC-0.11（探针盲区的静态兜底）** 对 12 个模块名逐个 `grep -rn --include='*.py'` 扫 `ouroboros/ supervisor/ server.py`，交出**完整命中清单**，且**每一处命中**都标注处置结论（`severing` / `relocating` / `explicitly-unavailable` / `not-a-target`）。判定：不允许存在未被覆盖的条目。**为什么必需**：AC-0.1/0.2/0.3 的探针只覆盖三个入口的 **import 期传递闭包**，下列两类命中在那时**根本不会执行**因此永远探不到——(a) **函数内局部 import**（`server_control.py:161-165` 的 panic 分支，且被 `except Exception: pass` 吞掉）；(b) **子进程 import 清单**（`update_merge.py:1085-1088` 的 post-apply smoke，路由崩会变成「应用成功但 smoke 失败」→ 静默回滚）。另需逐条判定：`gateway/onboarding.py:441-446`、`delegate_containment.py:132/184`、`subagents.py:419-491`、`review_thread_continuity.py`、`tools/delegate.py`、`tools/plan_review_runtime.py`。

### constraints

- 主规格 §3 的九条宪法约束逐条适用（尤其 P1「never silent truncation」、P7「Minimalism is about code, not capabilities」）。
- **探针集与保护集如上表逐字列出**——约束里必须带这两张清单，否则执行器拿不到删除目标全貌（`context_references` 不被 `session_context` 接受，规格路径也传不进去）。
- 本阶段额外：**不删任何文件**；**不动 `run_llm_loop` 的形态**；**不动 `consciousness` 的角色边界与工具上限**（主规格 §5.13）。
- 每个被删模块的 import 点必须**显式处理**，禁止新增吞异常的写法。
- `size_ratchet_manifest.py` 的 `GIANT_PATHS` 是单向字节棘轮；改动其中文件必须同 commit 更新 manifest。

### decisions

- 禁用「`except Exception: pass` 包住 import」这一既有写法（主规格 §9.2 纪律 1）。
- `endpoint_index.py` 是契约表，改动必须同 commit（§9.2 纪律 2）。
- 降级契约取「显式不可用」；HTTP 503 是本仓既有 unavailable 码；工具层返回标记字符串而非抛异常（依据见 §6.2）。
- 异常类型必须定义在删除后仍存在的模块里——`ouroboros/errors.py::OuroborosUnavailableError(RuntimeError)`。

---

## 3. Seed 1 —— 记忆外接 Engram

### goal

把情景/语义记忆外接 Engram（MCP stdio 独立通道），保留 `identity.md` / `patterns.md` / `improvement-backlog.md` 为本地文件与写入 SSOT，并按主规格 §5.15 的记忆模型实现有界工作记忆、冲突裁决分界与缺口通道。

### acceptance_criteria（骨架——**待 §5 的第 1 项定稿后补全**）

- **AC-1.1**（往返）一次会话 `mem_save` 一条知识 → 重启运行时 → 新会话 `mem_search` 取回。
- **AC-1.2**（读路径，§5.5）写入一条 `scope: global` 的 observation → 在**任意 project** 的任务里被召回；project 级观察不串味。
- **AC-1.3**（会话映射，§5.15）**并发跑两个任务**，两者都写记忆 → **两次写入都成功**（证明显式 `session_id` 生效、未落进 Engram 的 `fails closed when multiple candidates remain`）；任务终点后该 session 在 `sessions/recent` 可见且带 summary。
- **AC-1.4**（捕获期，§5.4）同一次任务中 max 与 low 两次投影的 `core_sha256` 一致。
- **AC-1.5**（降级，§5.11）停掉 Engram → tier-0 的 Engram 块显示**显式缺口标记**，且 `identity` / `patterns` / `improvement-backlog` **仍然渲染**。
- **AC-1.6**（迁移，§5.14）迁移后原文件仍在原位（逐文件断言）；`mem_search` 能命中迁移前 `knowledge/` 的已知 topic。
- **AC-1.7**（工作记忆有界，§5.15-a）连续 N 次 scratchpad 更新后，该 topic 的字节数**不单调增长**。
- **AC-1.8**（冲突裁决，§5.15-b）构造一次命中候选的 `mem_save` → `judgment_required: true` → agent **确实调用** `mem_judge`；随后 `mem_search` 带出 `supersedes:` / `conflicts:` 注解。
- **AC-1.9**（缺口通道，§5.15-c）造一个缺口 → 它出现在 tier-0，且 BG 的 `update_identity` 返回 `IDENTITY_UPDATE_ABSTAINED`。
- **AC-1.10**（自迭代链，§5.9）一次触发反思的任务后，`improvement-backlog.md` 出现新候选，且 `maybe_promote` 的输入仍来自 `reflection_entry`。
- **AC-1.11**（反例，§5.2）把 Engram 条目的 MCP `enabled` 改 false → **不影响** tier-0（独立通道生效）。

### constraints

同 §3 九条 + §5 的全部约束 + **P1 的 `scratchpad` 点名**（`BIBLE.md:73-76`）。

### decisions

§12.1/§12.2 台账；§5.5 用 `scope` 不用固定 project；§15.1 的「记忆外置属能力变更」。

---

## 4. Seed 2 / 3 / 4 —— 骨架与阻塞项

### Seed 2 — 删计费投影

**goal**：删除计费**投影层**（`pricing` 的定价部分 / `cost_projection` / `_usage_*` / cost-breakdown 路由 / web 成本面），**保留**物理发送托管与 evolution 的节流功能（换非货币实现）。

**AC 骨架**：正常任务不抛 `BudgetExceeded`；cost-breakdown 路由 404；**托管回归**（构造 dispatched/unresolved 的发送，同模型重试仍被禁止，`loop_llm_call.py:799-802` 行为不变）；终止出口 7 类逐条可触发；evolution 的 5 个存活闸门逐条验证；**BG 不再被永久判 `budget_blocked`**。

**阻塞（4 项，见 §5）**：BG `_check_budget` 替代形态；owner 钱面替代形态；cycle 上限 N 与用尽后语义；`pricing.py` 的 `infer_*` 迁出落点。

### Seed 3 — Claudexor → omp

**goal**：只裁 `AGENT_SESSION` 路由的 Claudexor 后端，改由 `omp --mode rpc` 常驻会话承担；`API_CHAT` 与 native 分支不动。

**AC 骨架**：`AGENT_SESSION` 路由产出 review；`API_CHAT` 与 native 分支**不变**（回归）；harness 后端缺失时走 `auto → native child`；**custody 三条件**（§7.3）逐条验收，尤其 `daemon_says_absent` 的正向「不存在」回答。

**阻塞（1 项）**：`omp --resume <不存在的 id>` 的退出码/错误码实测——**这是唯一可能超出「删文件 + 改接口」范围的点**。

### Seed 4 — 面轴收敛（角色规格）

**goal**：把已有的 `task_type` 认知面轴收敛成一处声明，并把 `delegation_role="background"` 迁回它；轴 A（血缘）与轴 C（呈现）不动。

**AC 骨架**：每个面的 effort / telemetry category / post-processing skip / 工具闸门在改造前后**逐项相等**；`scope_review` 双拼写统一；未知面不再静默退回 `medium`；BG 的行为不变。

**阻塞（1 项）**：必须先完成全仓 150 处 `delegation_role` 消费者按轴分类。

---

## 5. 阻塞项清单（不定这些，Seed 1/2/3/4 都不是 A-grade）

| # | 阻塞项 | 阻塞 | 谁能定 |
|---|---|---|---|
| 1 | 记忆工具改名与 BG 读证钩子的三选一（§5.6.1） | A、D | Owner（涉及 P0 边界） |
| 2 | BG `_check_budget` 的替代形态（§6.5） | B | Owner（涉及 P0） |
| 3 | owner 可见钱面的替代形态（§6.10） | B | **Owner**（P1/P8 明文的 agency 面） |
| 4 | evolution 的 cycle 上限 N，及用尽后「暂停」还是「完成」 | B | Owner |
| 5 | §12.2 的三项宪法级判断 | 全部 | **需 P0 程序，`ooo auto` 无法代解** |
| 6 | `omp --resume <不存在 id>` 的实测 | C | 一次实验即可 |

**interview 能解其中一部分**（4 的取值、A 的阈值形态），但 **2/3/5 是宪法级，不能在执行流程里顺手定**。

---

## 6. 给 `ooo auto` 的调用建议

### 6.1 实测：`ooo auto` 需要 10 个 section，而 AC 的**唯一可行入口**是 `session_context`

用 Seed 0 实跑了一次 `ooo auto`（`skip_run: true`，session `auto_44a0cd52588e`），ledger 显示它要求 10 个 section：

| section | 实测状态 |
|---|---|
| `goal` / `constraints` / `non_goals` / `runtime_context` | ✅ 被吸收（来自 goal 正文 + `user_preferences`） |
| `verification_plan` | ✅ 1 条（它把我写在 goal 正文里的 AC-0.4 归到了这里） |
| **`acceptance_criteria`** | ❌ **0 条** |
| **`actors`** | ❌ 0 |
| **`inputs`** | ❌ 0 |
| **`outputs`** | ❌ 0 |
| **`failure_modes`** | ❌ 0 |

**三条结论，第 2 条是修正后的**：

1. **AC 不能写在 goal 正文里**——实测不被结构化吸收（我写在正文里的 7 条只有 AC-0.4 被收进 `verification_plan`，`acceptance_criteria` 仍为 0 条）。

2. ⚠️ **也不要用 `user_preferences` 送 AC**（这一条推翻本文件早先的写法）。实测它会**挤进问答流**：当某个 section 恰好有 pending 问题时，你送去的该 section 会被**当作那个问题的答案**记入 `Recent auto answers`——`round 2/3/4 [user_preference]` 的 `A:` 字段就是我的整份 AC 列表，而问题是关于「不可用契约」这一个点的。后果是访谈在同一个点上反复追问，ambiguity 逐轮上升 **0.21 → 0.33 → 0.35**，最终在 phase deadline 上以 B 级部分产物收场。更糟的是我发过**两版** AC-0.8（第一版散文、第二版带逐字 tuple），两版在 ledger 里并存，访谈因此报出「同一面给出了三套互相矛盾的 503 写法」——**那个矛盾是我自己造成的**。

3. **唯一正确路径是 `generate_seed` 的 `session_context`（无访谈）**——AC 直接成为 Seed 的 `acceptance_criteria` 条目并带 `semantic_ac_key`，不经过问答流，也不与任何 pending 问题竞争。Seed 1/2/3/4 一律走这条。

（附带：`user_preferences` 送 `constraints` / `non_goals` / `runtime_context` 时**没有**被当作答案——因为当时没有针对这些 section 的 pending 问题。所以它是**条件性有害**：只要该 section 有 pending 问题，就会被消费成答案。结论不变：别用它送 AC。）

### 6.2 实测暴露的 Seed 0 缺口：降级契约

`ooo auto` 的 round 1 问题**指出了一个 Seed 0 的真实漏洞**（我没有写）：

> 切断后，那些被删模块原本支撑的运行时能力（usage 计费查询、cost 成本投影、memory 读写、ClaudeX 进程组委托）在模块缺席时该以什么契约呈现——静默降级、显式报「功能不可用」，还是端点与工具入口一并摘除？

**本规格的答案（依据 §9.2 的纪律 1 + AC-0.4）**：

| 面 | Seed 0 的契约 | 依据 |
|---|---|---|
| 工具 | **显式「不可用」**，不静默降级 | §9.2 纪律 1「所有被删模块的 import 点必须显式处理」；静默降级等于制造隐性故障 |
| HTTP 端点 | **保留注册**，处理器短路返回 typed 错误 | AC-0.4 要求 `HTTP_ENDPOINTS` 与 `collect_routes()` 逐条相等——摘端点属后续 seed 的活（那时连同契约表一起改） |
| 降级痕 | 记一条结构化日志/事件，不吞 | 与「不静默截断」同源 |

**须补进 Seed 0 的 AC**（**已以 committed 形态落进 §2 与 `seed-0-import-graph.yaml`，这里不再复述散文版**）：

- **AC-0.8** 三条逐字契约（HTTP 503 body 形状 / 异常类型与定义位置 / 工具缺席时的返回），**以「断言命令 + 期望结果」的形态**给出，见 §2 与 seed 文件。
- **AC-0.9** `collect_routes()` 在改动前后输出同一个整数——**以命令 + 期望不变量的形态**给出。

> ⚠️ 本节早先的散文版 AC 已被 committed 版取代。散文版就是 §6.1 第 2 条里那个「被 `user_preferences` 消费成答案、且两版并存造成自相矛盾」的来源。

### 6.3 调用方式（避开 30s MCP 超时）

**实测：`ooo auto` 必然 MCP 超时**——客户端限制 30s，而 ooo 自身 pipeline deadline 默认 7200s。超时后 `ooo auto` **仍在服务端继续执行**（本次实测：会话文件 `~/.ouroboros/data/auto_44a0cd52588e.json` 持续更新），返回的是「outcome unknown」而不是失败。

**因此**：

- **绝不能用同样参数重发**——会造重复 run。收口用 `resume`（带 auto session id）或 `reconcile_run: true`。
- **权威读取入口是 CLI，不是翻文件**：`ooo status auto <auto_session_id>`（id 形如 `auto_<hex>`，可从 `ooo status project <dir>` 取）。它给的是 event store 的权威视图，含 `Phase` / `Terminal` / `Last progress` / `Pending question` / `Recent auto answers` / `IntentGuard`。会话状态的**权威存储在 event store（SQLite，`ouroboros.persistence.event_store` + `resolve_event_store_path()`）**；`~/.ouroboros/data/*.json` 是同源的旁路快照，可读但不是权威。
- `ouroboros_project_status` 对 `skip_run` 的会话显示 `Runs: 0` 是**正常的**——它统计的是 run，而 `skip_run` 停在 Seed。

### 6.4 范围建议

- **Seed 0 现在就能跑**：阻塞项为零，且是 Seed 1/2/3 的前置。
- **Seed 1/2/3/4 建议走 `generate_seed` 的无访谈路径**（`session_context`），而非 `ooo auto` 的访谈路径——见 §6.5 的实测依据。
- **不要**把 Seed 0+1+2+3 合成一个 goal：AC 无法穷举，A-grade 门过不去，执行会无界。

### 6.5 实测对比：访谈路径 vs 无访谈路径

同一个 Seed 0，两条路径都跑过：

| | `ooo auto`（访谈路径） | `generate_seed`（无访谈） |
|---|---|---|
| Seed | `seed_a9a10a35dff0` | **`seed_934e30c1d249`**（已存 `docs/superpowers/specs/seed-0-import-graph.yaml`） |
| 等级 | **B** | 歧义 0.20（结构性上限，非评分）；`degraded: false` |
| `unresolved_slots` | **`[acceptance_criteria]`** | **`[]`（零）** |
| 中断原因 | `Partial product: yes (reason: interview_phase_deadline)` | —— |
| AC 是否结构化 | ❌（只把 AC-0.4 收进 `verification_plan`） | ✅ 10 条全带 `semantic_ac_key` |

**结论：Seed 1/2/3/4 一律走 `session_context`。** 但**理由不是「访谈路径在这个仓收敛不了」**（那是我的错误归因），而是：**访谈跑满了它的 10 轮预算、自我裁决了 13 项、只在 `acceptance_criteria` 这一项上没收敛——而 AC 恰好就是我从错误通道（`user_preferences`）送进去的那一项。** 详见 §6.5.1 的权威记录与 §6.1 第 2 条的机制。

### 6.5.1 根因是 AC 的**形态**，不是内容

**权威记录**是访谈 trace（`<repo>/.ouroboros/traces/auto_44a0cd52588e/summary.md`，由 `ooo auto` 写在项目目录里）：

```
Status: complete · Grade: B · Seed: seed_a9a10a35dff0 (origin: auto_pipeline)
Questions: 10 · Decisions: 15 (promoted 13, rejected 2) · Flags: 4
Decision provenance: maintainer_policy 3, user_confirmed 10
Open gaps: acceptance_criteria
```

`flags.jsonl` 给出闭环原因：`closure_route: partial_seed_from_evidence`、`ledger_ready: False`、`degraded_seed` 且 `recovery_reason: interview_phase_deadline`。

**`questions.jsonl` 的逐条正文长度（实测）**：

```
 1. len=173   切断后，那些被删模块原本支撑的运行时能力……该以什么契约呈现
 2. len=501   被切断能力在模块缺席时的「显式不可用」契约，本轮上下文里同一面给出了三套互相矛盾的写法……
 3. len=501   (ambiguity: 0.21) AC-0.8 的断言（status≠200 且 body 含 "unavailable"）对三种候选 503 body 全通过，测试因此欠定……
 4. len=396   (ambiguity: 0.33) AC-0.8 对 POST /api/cost-breakdown 的断言……同样全通过
 5..10. len=0   ← 空
```

**以及轮预算（实测 `~/.ouroboros/config.yaml`）**：`clarification.max_interview_rounds: **10**`——它**覆盖**了 auto 会话里记的 50。所以访谈是**跑满预算**的。

**准确表述**：

> 它在**同一个点**上连问 **4 轮未收敛**（ambiguity 0.21 → 0.33）→ 随后 **6 个空槽空转** → 由 phase deadline 以 **partial seed** 收场。

两个我先前说错的表述：❌「停在 round 1」——那是被访谈会话文件的 `rounds: 1` 误导；❌「跑了 10 个有效问题」——其中 6 条正文为空。

**`Decision provenance: user_confirmed 10`** 则是我 `user_preferences` 污染的量化证据：我那批偏好被记成了 10 条「用户确认的决策」，其中就包括把我整份 AC 列表当成某个具体问题的答案。**访谈第 3/4 轮抱怨的「三套互相矛盾的 503 写法」，源头是我自己发过两版 AC-0.8**（第一版散文、第二版带逐字 tuple），两版在 ledger 里并存。

**形态要求**（访谈应答器的系统提示）：

> ONE concrete, committed, testable decision — **specific values, exact commands/flags, a small sample input and its exact expected output, and explicit error/stderr/exit-code behavior**.

散文式行为描述落到 ledger 只算 `[user_preference]`，**不算 committed** → 访谈必须继续追问。

**对照**：

| 形态 | 例 |
|---|---|
| ❌ 散文 | 「`python -c "import server"` 退出码 0；且把模块临时移走后仍退出码 0」 |
| ✅ committed | 「运行 `for m in <12 个模块>; do mv "$m" /tmp/probe_moved.py; python -c "import server" >/dev/null 2>&1; rc=$?; mv /tmp/probe_moved.py "$m"; echo "$m EXIT=$rc"; done` 期望：输出 **12 行**，每行以 `EXIT=0` 结尾；行数少于 12 即失败」 |

### 6.5.2 `session_context` 的键：`project_type` 有效，`context_references` **无效**

第二个 seed 的 `brownfield_context`：

| 字段 | 第一版（我传 `project_type: "coding"`） | 第二版（我传 `project_type: "brownfield"`） |
|---|---|---|
| `project_type` | ❌ `greenfield`（默认） | ✅ `brownfield` |
| `context_references` | `[]` | **仍 `[]`** —— 该键**不被接受**（schema 也没列它） |
| `task_type` | `code` | `code` |

**所以**：`project_type` 是有效键且必须显式给 `brownfield`（否则默认 `greenfield`，执行器会丢掉「先找既有模式与既有消费者」的信号——而这正是 AC-0.5 与「不得把 `semantic_dedup.py` / `tools/knowledge.py` 当整删模块」要防的事）。`context_references` 当前不接受，规格路径要写进 `goal` 或 `constraints` 里。

### 6.6 `ooo auto` 与 `generate_seed` 的会话/产物位置

| 对象 | 路径 |
|---|---|
| auto 会话状态 | `~/.ouroboros/data/auto_<id>.json` |
| 访谈会话状态 | `~/.ouroboros/data/interview_<id>.json` |
| 访谈状态机库 | `~/.ouroboros/data/ouroboros.db` |
| **访谈路径产出的 Seed** | `~/.ouroboros/seeds/seed_<id>.yaml`（自动落盘） |
| **无访谈路径产出的 Seed** | ⚠️ **只在工具有返回体里，不自动落盘**——必须自己存（本仓存在 `docs/superpowers/specs/seed-0-import-graph.yaml`） |

### 6.7 已证伪的报告（记录下来，避免后续会话重复争论）

本文件写作过程中收到过两份与磁盘不符的审计报告，**均经逐条核实后否决**：

| 报告 | 声称 | 磁盘实测 |
|---|---|---|
| A | `seed-0-import-graph.yaml` 的 `semantic_ac_key` **整体错位**，`AC-0.8` 出现两次且互相矛盾，并给出两个「文件里存在」的 key | **当时**确实逐条与返回体一致（9 条、各一 key、`AC-0.8` 仅 1 次）；那两个 key 不存在。报告随后**自行撤回** |
| B | 新 seed 落盘后，`AC-0.2`…`AC-0.7` 仍是**旧 seed 的 key**，只有 AC-0.1/0.8/0.9 是新的；理由是返回体在 AC 块有 `[…20ln elided…]` | **新 seed 的 key 共 9 个，旧 seed 的 key 共 0 个**；`seed_id: seed_a8af8b9801e5`；返回体 9 条 AC 与 key 当时全部可见，无 elision。报告随后**自行撤回**（「我那次 grep 读到的是覆盖前的瞬时状态，不是终态」） |

**纪律**：收到具体到 file:line / 具体到字符串的指控时，**一律先对磁盘取一次证再动**——这两次都是「先核实」避免了改坏一个本来正确的文件。反之，形态类结论（AC 的形态、`brownfield_context` 默认值、`user_preferences` 污染问答流）三次全部为真，也都已在 §6.1 / §6.5.1 / §6.5.2 落地。
