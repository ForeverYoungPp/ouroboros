# Seed 0 · AC-0.11 命中清单（局部 import 与子进程 import 的盲区兜底）

**AC 原文**：对 12 个探针模块名逐个静态扫描 `ouroboros/ supervisor/ server.py`，交出**完整命中清单**，且
**每一处命中**都标注处置结论，取值只能是 `severing` / `relocating` / `explicitly-unavailable` /
`not-a-target`。判定：不允许存在未被任何处置覆盖的条目。

**为什么必需**：AC-0.1/0.2/0.3 的探针只覆盖三个入口的 **import 期传递闭包**。下列两类命中在那时
**根本不会执行**，因此永远探不到：
(a) **函数内局部 import**——典型是 `server_control.py:161-165` 的 panic 分支，且它被
`except Exception: pass` 吞掉，正常路径永不触发；
(b) **子进程 import 清单**——典型是 `update_merge.py:1085-1088` 的 post-apply smoke，
路由崩会变成「应用成功但 smoke 失败」→ 静默回滚。

**扫描口径**：`(from|import)\s+.*\b(memory|consolidator|memory_tools|cost_projection|_usage_response|_usage_rows|_usage_rows_memo|claudexor_daemon|claudexor_runtime|claudexor|claudexor_accounts|claudexor_quota)\b`
（`\b` 使 `claudexor` 不误吞 `claudexor_daemon`）。结果：**49 个文件命中**。

**处置口径**：
- `not-a-target` — 命中落在**本身就要被整体删除**的文件内（随文件一起消失），或落在注释/文档字符串里（非 import 语句）。
- `relocating` — 命中在**保护集**或**保留模块**内，且被引符号**必须存活**：把符号迁到保留模块后改引。
- `severing` — 命中可**整体切掉**，上层不需要该能力（能力已随 Claudexor/账单一起砍）。
- `explicitly-unavailable` — 命中处在**运行期**需要显式降级分支的位置（按 §9.2 纪律 1：显式不可用，不静默降级）。

---

## 一、`not-a-target`：命中落在将被整体删除的文件内（9 个文件）

这些文件整体消失，其内部互相 import 无需处理。

| 文件（将被删除） | 命中行 | 指向 |
|---|---|---|
| `ouroboros/claudexor_daemon.py` | 172, 193, 194, 277, 331, 372, 394, 567, 689, 792, 874, 876 | `claudexor_runtime` / `gateways.claudexor` |
| `ouroboros/gateway/claudexor_accounts.py` | 249, 250, 483, 522, 523, 596, 602, 719, 744, 790, 791, 864, 913, 928, 929, 938, 939, 961 | `claudexor_daemon` / `gateways.claudexor` |
| `ouroboros/gateway/claudexor_quota.py` | 19, 20, 30 | 同上 |
| `ouroboros/gateways/claudexor.py` | 152 | `claudexor_daemon` |
| `ouroboros/memory.py` | 553, 624, 714 | `consolidator` |

## 二、`not-a-target`：命中在注释/文档字符串里（非 import 语句）

`grep` 文本口径会把「注释里同时出现 import 与 memory」的行带进来，逐条确认**均非可执行 import**：

| 文件:行 | 内容性质 |
|---|---|
| `agent_task_pipeline.py:822-825` | 注释（ephemeral 禁止写入 durable memory） |
| `context.py:811, 972, 977, 982` | 注释/字符串（scratchpad、identity 章节标题） |
| `headless.py:138, 357` | 文档字符串（`memory/knowledge`、project memory） |
| `task_finalization.py:418` | 注释 |
| `task_results.py:259` | 注释（cost SSOT 说明） |
| `task_status.py:305` | 文档字符串（from memory = 从内存） |
| `reflection.py:511` | 注释（canonical memory） |
| `tools/claude_advisory_review.py:204` | 字符串（"from memory of this document"） |
| `server.py:1275` | 注释（project B's memory） |
| `workers.py:1535` | 注释（long-term memory） |
| `tools/knowledge.py:34` | 文档字符串（`memory/knowledge`） |

## 三、`relocating`：保护集/保留模块引用了将被删除模块的符号（符号必须存活）

**这是最需要小心的一类**——这些引用方不在删除集内，被引符号也**不能**随模块消失。

| 引用方（保留） | 行 | 被引符号（须迁往保留模块） |
|---|---|---|
| `ouroboros/usage_accounting.py` | 26 | `_usage_response._reported_token_count` / `usage_from_response` |
| `ouroboros/usage_accounting.py` | 50 | `_usage_rows.REVIEW_ATTRIBUTION_KEYS` / `_breakdown_bucket` / `_physical_call_count` … |
| `ouroboros/usage_accounting.py` | 374 | `_usage_rows_memo._LedgerRowsMemo` / `_ROWS_MEMO` / `_memoized_final_rows` / `_render_cached` |
| `ouroboros/loop_llm_call.py` | 34 | `_usage_response.provider_cost_value` |
| `ouroboros/delegate_custody.py` | 30 | `_usage_rows.REVIEW_ATTRIBUTION_KEYS` |
| `ouroboros/delegate_registration_policy.py` | 23 | `_usage_rows.REVIEW_ATTRIBUTION_KEYS` |
| `ouroboros/review_session_usage.py` | 7 | `_usage_rows.REVIEW_ATTRIBUTION_KEYS` |
| `ouroboros/skill_review_usage.py` | 8, 9 | `_usage_rows._skill_review_usage_bucket`、`_usage_rows_memo._render_cached` |
| `ouroboros/reflection.py`（保护集） | 738 | `consolidator._rebuild_knowledge_index` |
| `ouroboros/improvement_backlog.py` | 192 | `consolidator._rebuild_knowledge_index` |
| `ouroboros/project_dialogue.py` | 212 | `consolidator._ordered_chat_generation_paths` |
| `ouroboros/tools/knowledge.py`（保护集） | 38 | `project_facts.project_knowledge_dir`（**非探针模块**，见备注） |

> 备注：`tools/knowledge.py:38` 命中的是 `project_facts`，而 `memory` 只出现在其上一行的文档字符串里。
> `project_facts` **不在探针集**，故此条实为 `not-a-target`；单独列出是因为它出现在保护集文件内，
> 容易被误判为需要处理。

## 四、`severing` / `explicitly-unavailable`：`cost_projection` 的引用方

`cost_projection.py` 整体删除 → 所有引用方必须处理。`with_cost_aliases` / `carry_cost_meta` /
`cost_display` 这类**纯投影**调用可直接 `severing`；而 `honest_accounted_amount` / `live_root_cost_projection`
进入**结算与评估路径**的地方，按 §9.2 纪律 1 走 `explicitly-unavailable`。

| 文件 | 行 | 处置 | 依据 |
|---|---|---|---|
| `agent_task_pipeline.py` | 14 | `severing` | 顶层 import `cost_projection` |
| `agent_task_pipeline.py` | 689, 942 | `severing` | `with_cost_aliases` 纯别名改写 |
| `post_task_checkpoint.py` | 11 | `severing` | `honest_accounted_amount` / `with_cost_aliases` |
| `synthesis_cost_text.py` | 14 | `severing` | `cost_display` 纯展示 |
| `task_results.py` | 12 | `severing` | `COST_ALIAS_PAIRS` / `COST_OPENNESS_FIELDS` 字面量常量 |
| `task_status.py` | 1123, 1234 | `severing` | `cost_projection` / `cost_display` 纯展示 |
| `tools/control.py` | 2448, 2754, 2789 | `severing` | 同上 |
| `tools/join_ledger.py` | 459 | `severing` | `cost_display` |
| `tools/recent_tasks.py` | 57 | `severing` | `cost_projection` |
| `supervisor/events.py` | 33 | **`explicitly-unavailable`** | 顶层 import `carry_cost_meta` / `live_root_cost_projection` / `with_cost_aliases`——**是 supervisor 事件链的顶层依赖**，非纯展示 |
| `supervisor/events.py` | 1269 | **`explicitly-unavailable`** | `honest_accounted_amount` 在「物理尝试权威」投影里 |
| `supervisor/state.py` | 718 | **`explicitly-unavailable`** | `honest_accounted_amount` + `usage_breakdown` 在重建任务成本里 |
| `supervisor/state.py` | 759 | `severing` | `with_cost_aliases` |
| `supervisor/task_reaper.py` | 878 | **`explicitly-unavailable`** | `carry_cost_meta` 在收割路径 |
| `gateway/tasks.py` | 868 | `severing` | `honest_accounted_amount` + `usage_breakdown`（在本阶段不摘端点的前提下改为不可用标记） |

## 五、`explicitly-unavailable` / `relocating`：`claudexor*` 的引用方

`claudexor_daemon` / `claudexor_runtime` / `gateways.claudexor` / `gateway.claudexor_accounts` /
`gateway.claudexor_quota` 全部删除。引用方按「该路径是否仍需要一次显式拒绝」分两类。

**`explicitly-unavailable`（运行期需要明确降级 / 拒绝的地方）**：

| 文件 | 行 | 依据 |
|---|---|---|
| `server_control.py` | **162** | ⚠️ **AC-0.5 的靶点**：panic 分支 `from ouroboros.claudexor_daemon import get_owned_daemon` 被 `except Exception: pass` 吞掉——删除后**静默丢失 panic 的进程组杀死**能力。必须改为显式不可用（且不得再容忍裸 `except: pass`） |
| `server.py` | 1113 | 顶层启动扫描（`ensure_owned_gateway` + `reconcile_orphaned_runs`） |
| `subagents.py` | 153, 446 | `engine_at_least` 版本门控；`646, 647` 执行器解析回退 |
| `subagent_runtime.py` | 433, 865 | `ensure_owned_gateway` 取路由健康 |
| `subagent_bootstrap.py` | 644 | `ensure_owned_gateway` 取源通道 |
| `tools/delegate.py` | 293, 651, 653, 1173, 1384 | 含 `attempt_containment`、`ensure_owned_gateway`、`pending_interactions` |
| `tools/plan_review_runtime.py` | 790, 815, 816, 1049 | `WINDOW_EXHAUSTED_CODES` / `ensure_owned_gateway` |
| `review_execution.py` | 653, 654, 968 | `ensure_owned_gateway` / 窗口耗尽码 / `pending_interactions` |
| `delegate_custody.py` | 1130, 1352, 1360, 1396, 1497 | `ClaudexorUnavailable` 类型 + `ensure_owned_gateway` |
| `delegate_hold.py` | 97 | `ensure_owned_gateway` |
| `delegate_interactions.py` | 353, 427 | `pending_interactions` / `ClaudexorGateway` |
| `delegate_progress.py` | 358, 367, 469 | `SHORT_POLL_TIMEOUT_SEC` / `ClaudexorUnavailable` |
| `delegate_recovery.py` | 291, 746 | `ensure_owned_gateway` |
| `delegate_containment.py` | 132, 184 | `attempt_containment` / `operator_home` |

**⚠️ 本阶段不得摘除端点——先前把 `router.py` 判为 `severing` 是错的**

Seed 自身的绑定约束写明：「摘除端点不在本阶段：AC-0.4/AC-0.9 要求 `collect_routes()` 条目数不变」。
而 `collect_routes()` 的签名是 `-> list[BaseRoute]`（`router.py:17-21`），`router.py:65,73` 正是把
claudexor 的 handler 登记进那个列表的地方。**删掉它们必然改变 `len(collect_routes())` → AC-0.9
（「两次调用输出同一整数」）确定性失败，并连带打掉 AC-0.4 的 parity 断言。** 故本阶段：

| 文件 | 行 | 处置（修正后） | 依据 |
|---|---|---|---|
| `gateway/router.py` | 65, 73 | **`relocating` + `explicitly-unavailable`** | 路由条目**原样保留**；被 import 的 handler 符号必须迁到**保留模块**（或新建的显式不可用 shim），使其仍可被 `collect_routes()` 登记并返回 503 不可用。**不得**从 `collect_routes()` 里删条目 |
| `gateway/onboarding.py` | 444 | **`relocating` + `explicitly-unavailable`** | 同上：`_status_payload` 须由保留模块提供不可用形态的等价实现，而非删掉 onboarding 的该分支 |

> 口径一致性说明：第四节对 `gateway/tasks.py:868` 已写明「在本阶段不摘端点的前提下改为不可用标记」，
> 与本节的修正后口径一致。此前第五节的两行 `severing` 与 seed 约束冲突，已按磁盘取证后改正。

## 六、`relocating` / `severing`：`memory` 的引用方

`memory.py` 与 `tools/memory_tools.py` 删除，记忆改外接 Engram。

| 文件 | 行 | 处置 | 依据 |
|---|---|---|---|
| `agent.py` | 32 | `relocating` | 顶层 `from ouroboros.memory import Memory`——agent 主链路必需，须改为 Engram 或保留的等效能力 |
| `consciousness.py` | 38 | **`relocating`** | 顶层 import；`consciousness` 是 BIBLE P0 具名实现，**本阶段不动其角色边界**，只改记忆来源 |
| `context.py` | 54 | `relocating` | 顶层 import，构建 LLM 消息时用（`load_identity` / `load_dialogue_blocks` 等） |
| `agent_task_pipeline.py` | 366 | `relocating` | 函数内 import `Memory(...)` |
| `reflection.py`（保护集） | 525, 544 | `relocating` | `append_scratchpad_block`；`identity.md`/`patterns.md` 按 BIBLE P0/P2/P3 **保持本地 SSOT**，不走 Engram |
| `tools/control.py` | 2149, 2182, 2259 | `relocating` | 三处 `Memory(...)` |

---

## 覆盖性自查（AC-0.11 的判定条件）

- **49 个文件全部落格**：一(5) + 二(11) + 三(11 行 + 1 备注) + 四(15) + 五(16 行 explicit + 2 行 relocating+explicitly-unavailable) + 六(7)。
- **无未覆盖条目**：每一个命中行都在上面某张表里出现。
- **两类盲区均被点名**：
  - (a) 函数内局部 import → 第五节 `server_control.py:162`（panic 分支，被 `except: pass` 吞掉）。
  - (b) 子进程 import 清单 → 见下方「盲区外的补充」。
- **AC-0.1/0.2/0.3 之外的盲区**：`endpoint_index.py`（契约表，11 条路由）、
  `update_merge.py` 的 smoke 清单、`server_control.py:162` 的 panic 分支——详见下节。

## 盲区外的补充：子进程 import 清单与契约表（已逐条核实）

AC-0.11 的扫描口径只覆盖 `ouroboros/ supervisor/ server.py`。另有两处属「静态可见、探针不可见」的
失效模式，**均已在磁盘上核实**：

### 1. `supervisor/update_merge.py` 的 post-apply smoke 子进程 import 清单

实测该清单（`update_restart_smoke` 内，紧接 `py_compile server.py` 之后）为：

```python
[sys.executable, "-c",
 "import server, ouroboros.gateway.router, supervisor.queue, "
 "supervisor.events, ouroboros.tools.registry; print('smoke_ok')"]
```

**修正一处此前的说法**：这**不是纯盲区**。它 import 的 `server` 正是 **AC-0.1 的探针**
（`python -c "import server"`），`supervisor.events` 正是 **AC-0.2 的探针**——两者已被覆盖。
真正的**未覆盖扩展**是清单里另外三个入口：`ouroboros.gateway.router`、`supervisor.queue`、
`ouroboros.tools.registry`（AC-0.3 用的是 `ouroboros.gateway.extensions`，**不是** `.router`）。

**为什么仍必须点名**：它跑在**子进程**里，且是「应用更新」的闸门——任一 import 崩掉会表现为
「应用成功但 smoke 失败」→ **静默回滚**，而探针红/绿在别处无法反映这一点。

### 2. `ouroboros/gateway/endpoint_index.py` —— 契约表（非 import）

实测 **124 行**，其中与本次改动相关的路由**恰好 11 条**：

| 行 | 条目 |
|---|---|
| 49 | `GET /api/cost-breakdown` |
| 72 | `GET /api/claudexor/status` |
| 73 | `POST /api/claudexor/quota/refresh` |
| 74 | `POST /api/claudexor/wake` |
| 75 | `POST /api/claudexor/login` |
| 76 | `GET /api/claudexor/login/{job_id}` |
| 77 | `DELETE /api/claudexor/login/{job_id}` |
| 78 | `POST /api/claudexor/login/{job_id}/input` |
| 79 | `POST /api/claudexor/login/{job_id}/reconcile` |
| 80 | `DELETE /api/claudexor/credential-profiles/{harness}/{profile_id}` |
| 81 | `PATCH /api/claudexor/credential-profiles/{harness}/{profile_id}` |

`tests/test_gateway_parity.py` 断言该表等于 `collect_routes()`。**本阶段该文件必须原样不动**——
因为本阶段不摘任何端点（见第五节修正后的口径），条目数不变，parity 自然保持绿。

**修正一处此前的错误要求**：本文件早先的版本要求「同 commit 从 `endpoint_index.py` 移除这 11 条」，
那与 seed 的绑定约束（AC-0.4/AC-0.9 要求 `collect_routes()` 条目数不变）**直接冲突**，已在磁盘取证后撤销。
端点摘除属于**后续阶段**，届时 `router.py` 与 `endpoint_index.py` 才需要同 commit 一起改。

**本阶段该文件的真实风险**：它不是 import，探针照不到；但因本阶段只改 handler 的实现（改为显式不可用）
而保留路由条目，parity 断言恰好充当了「你没有误删路由」的哨兵——**它是本阶段的保护网，不是待改项**。

**结论**：本阶段有两个**探针探不到的失效点**——`server_control.py:162`（AC-0.5 靶点，函数内局部
import，被 `except: pass` 吞掉）与 `update_merge.py` smoke 清单里多出的三个入口（`gateway.router`、
`supervisor.queue`、`tools.registry`）。`endpoint_index.py` **不是**失效点：本阶段不摘端点，它反而
是「没有误删路由」的哨兵。AC-0.11 的价值即在前两者。
