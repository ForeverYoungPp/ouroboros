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
| **E** | 切 import 图（主规格 §9.2） | 无 | ✅ **阻塞项为零** |
| **A** | 记忆外接 Engram（§5，含 §5.15 记忆模型） | E | ❌ 1 项未决 |
| **B** | 删计费投影（§6） | E | ❌ 4 项未决 |
| **C** | Claudexor → omp（§7） | E | ❌ 1 项待实测 |
| **D** | 面轴收敛（角色规格 §4-§9） | A、B | ❌ 需先做 150 处分类 |

**为什么必须拆**：`ooo` 的 A-grade 门要求 AC 可穷举可判定；「把三件事都做完」这种 goal 的 AC 无法穷举，且执行会无界。writing-plans 的 Scope Check 也明说多子系统必须各成 plan，每份自己产出可运行可测的软件。

---

## 2. Seed E —— 切 import 图（**可直接开跑**）

### goal

在主规格 §9.1 删除任何模块之前，切断「保留下来的模块」对「将被删除的模块」的全部 import 依赖，使 server 与 supervisor 在被删模块缺席时仍能正常启动。**此阶段不删除任何文件。**

### acceptance_criteria

- **AC-E1** `python -c "import server"` 退出码 0；且把 `ouroboros/usage_accounting.py` **临时移走**后**仍**退出码 0。
- **AC-E2** `python -c "import supervisor.events"` 退出码 0；且把 `ouroboros/cost_projection.py` 临时移走后**仍**退出码 0（该模块当前在 `supervisor/events.py:33` 被顶层 import）。
- **AC-E3** `python -c "import ouroboros.gateway.extensions"` 退出码 0；且把 `ouroboros/_usage_rows.py` 临时移走后**仍**退出码 0（当前经 `skill_review_usage.py:10-12` 被硬绑）。
- **AC-E4** `pytest tests/test_gateway_parity.py` 全绿——`endpoint_index.HTTP_ENDPOINTS` 与 `collect_routes()` 逐条相等。
- **AC-E5** 对每个将被删除的模块名，全仓不存在「用 `except Exception: pass`（或 `except Exception` 后仅 log）吞掉其 import」的写法；`server_control.py:161-165` 已改为显式处理，且 panic 仍能终止委托 run 的进程组（由 `tests/test_server_control_panic_daemon.py` 覆盖）。
- **AC-E6** `web` 模块图不断：`web/modules/claudexor_status_store.js` 被 **7 个模块** import（含 `settings.js:21` 挂在 `app.js` 上）。改造后 `web/tests/` 的现有冒烟测试全绿，且不存在「删掉该文件导致整站白屏」的路径。
- **AC-E7** 本阶段 `git diff --stat` **不含任何文件删除**——只有 import 点与适配代码的改动。

### constraints

- 主规格 §3 的九条宪法约束逐条适用（尤其 P1「never silent truncation」、P7「Minimalism is about code, not capabilities」）。
- 本阶段额外：**不删任何文件**；**不动 `run_llm_loop` 的形态**；**不动 `consciousness` 的角色边界与工具上限**（主规格 §5.13）。
- 每个被删模块的 import 点必须**显式处理**，禁止新增吞异常的写法。

### decisions

- 禁用「`except Exception: pass` 包住 import」这一既有写法（主规格 §9.2 纪律 1）。
- `endpoint_index.py` 是契约表，改动必须同 commit（§9.2 纪律 2）。

---

## 3. Seed A —— 记忆外接 Engram

### goal

把情景/语义记忆外接 Engram（MCP stdio 独立通道），保留 `identity.md` / `patterns.md` / `improvement-backlog.md` 为本地文件与写入 SSOT，并按主规格 §5.15 的记忆模型实现有界工作记忆、冲突裁决分界与缺口通道。

### acceptance_criteria（骨架——**待 §5 的第 1 项定稿后补全**）

- **AC-A1**（往返）一次会话 `mem_save` 一条知识 → 重启运行时 → 新会话 `mem_search` 取回。
- **AC-A2**（读路径，§5.5）写入一条 `scope: global` 的 observation → 在**任意 project** 的任务里被召回；project 级观察不串味。
- **AC-A3**（会话映射，§5.15）**并发跑两个任务**，两者都写记忆 → **两次写入都成功**（证明显式 `session_id` 生效、未落进 Engram 的 `fails closed when multiple candidates remain`）；任务终点后该 session 在 `sessions/recent` 可见且带 summary。
- **AC-A4**（捕获期，§5.4）同一次任务中 max 与 low 两次投影的 `core_sha256` 一致。
- **AC-A5**（降级，§5.11）停掉 Engram → tier-0 的 Engram 块显示**显式缺口标记**，且 `identity` / `patterns` / `improvement-backlog` **仍然渲染**。
- **AC-A6**（迁移，§5.14）迁移后原文件仍在原位（逐文件断言）；`mem_search` 能命中迁移前 `knowledge/` 的已知 topic。
- **AC-A7**（工作记忆有界，§5.15-a）连续 N 次 scratchpad 更新后，该 topic 的字节数**不单调增长**。
- **AC-A8**（冲突裁决，§5.15-b）构造一次命中候选的 `mem_save` → `judgment_required: true` → agent **确实调用** `mem_judge`；随后 `mem_search` 带出 `supersedes:` / `conflicts:` 注解。
- **AC-A9**（缺口通道，§5.15-c）造一个缺口 → 它出现在 tier-0，且 BG 的 `update_identity` 返回 `IDENTITY_UPDATE_ABSTAINED`。
- **AC-A10**（自迭代链，§5.9）一次触发反思的任务后，`improvement-backlog.md` 出现新候选，且 `maybe_promote` 的输入仍来自 `reflection_entry`。
- **AC-A11**（反例，§5.2）把 Engram 条目的 MCP `enabled` 改 false → **不影响** tier-0（独立通道生效）。

### constraints

同 §3 九条 + §5 的全部约束 + **P1 的 `scratchpad` 点名**（`BIBLE.md:73-76`）。

### decisions

§12.1/§12.2 台账；§5.5 用 `scope` 不用固定 project；§15.1 的「记忆外置属能力变更」。

---

## 4. Seed B / C / D —— 骨架与阻塞项

### Seed B — 删计费投影

**goal**：删除计费**投影层**（`pricing` 的定价部分 / `cost_projection` / `_usage_*` / cost-breakdown 路由 / web 成本面），**保留**物理发送托管与 evolution 的节流功能（换非货币实现）。

**AC 骨架**：正常任务不抛 `BudgetExceeded`；cost-breakdown 路由 404；**托管回归**（构造 dispatched/unresolved 的发送，同模型重试仍被禁止，`loop_llm_call.py:799-802` 行为不变）；终止出口 7 类逐条可触发；evolution 的 5 个存活闸门逐条验证；**BG 不再被永久判 `budget_blocked`**。

**阻塞（4 项，见 §5）**：BG `_check_budget` 替代形态；owner 钱面替代形态；cycle 上限 N 与用尽后语义；`pricing.py` 的 `infer_*` 迁出落点。

### Seed C — Claudexor → omp

**goal**：只裁 `AGENT_SESSION` 路由的 Claudexor 后端，改由 `omp --mode rpc` 常驻会话承担；`API_CHAT` 与 native 分支不动。

**AC 骨架**：`AGENT_SESSION` 路由产出 review；`API_CHAT` 与 native 分支**不变**（回归）；harness 后端缺失时走 `auto → native child`；**custody 三条件**（§7.3）逐条验收，尤其 `daemon_says_absent` 的正向「不存在」回答。

**阻塞（1 项）**：`omp --resume <不存在的 id>` 的退出码/错误码实测——**这是唯一可能超出「删文件 + 改接口」范围的点**。

### Seed D — 面轴收敛（角色规格）

**goal**：把已有的 `task_type` 认知面轴收敛成一处声明，并把 `delegation_role="background"` 迁回它；轴 A（血缘）与轴 C（呈现）不动。

**AC 骨架**：每个面的 effort / telemetry category / post-processing skip / 工具闸门在改造前后**逐项相等**；`scope_review` 双拼写统一；未知面不再静默退回 `medium`；BG 的行为不变。

**阻塞（1 项）**：必须先完成全仓 150 处 `delegation_role` 消费者按轴分类。

---

## 5. 阻塞项清单（不定这些，A/B/C/D 都不是 A-grade）

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

- **Seed E 现在就能跑**：它阻塞项为零，且是 A/B/C 的前置。
- **A/B/C/D 建议走 `generate_seed` 的无访谈路径**（`session_context`），而非 `ooo auto` 的访谈路径——因为 goal / constraints / decisions 已在这两份规格里定稿，访谈只会在已定事项上打转。等 §5 的阻塞项定稿后再补 AC。
- **不要**把 E+A+B+C 合成一个 goal：AC 无法穷举，A-grade 门过不去，执行会无界。
