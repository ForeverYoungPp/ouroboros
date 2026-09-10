# Ouroboros 认知面（agent 职责）的设计

- 日期：2026-09-10
- 分支：`design/slim-down-engram`
- 状态：待复核
- 与主规格的关系：**独立规格**。主规格（`2026-09-10-ouroboros-slim-down-design.md`）§12.2 明确「`consciousness` 的任何改动另开一份规格」；本文是那一份。

## 0. 一句话

本仓有**三条轴**：血缘（`delegation_role`）、认知面（`task_type`）、呈现（`sender_identity`）。认知面轴**已经存在**且已承载 `evolution` / `review` / `deep_self_review` / `scope_review` / `consciousness` / `summarize`，但它的每面策略散落在至少 8 处、无枚举、无校验器。而**后台意识站错了轴**——它盖的是 `delegation_role="background"`（血缘轴），同时在认知面轴上还留着 `"consciousness"` 这个值。本设计把认知面轴收敛成一处声明，并把 BG 迁回它。

## 1. 现状

### 1.1 两个 agent 职责（实测）

| 维度 | 任务 agent（R1） | 后台意识（R2） |
|---|---|---|
| 权威来源 | task contract / owner 委派 | P0 agency（`BIBLE.md:42-45` 具名实现） |
| 循环 | `agent.py:1217 run_llm_loop` → `loop.py:6087` | 自有 `_loop:627` / `_think:694` / `_think_scoped:714`，**不调** `run_llm_loop` |
| 工具可见性 | 全量注册表 | **独立 `ToolRegistry` 实例**（`consciousness.py:1203`）+ `_BG_TOOL_WHITELIST`（`:1193`）在 `_execute_tool:1244` **强制** |
| 上下文 | `context.build_llm_messages(mode, task)` | 自有 `_build_context`（`partition="all"`） |
| 触发 | 队列准入 / owner 消息 / 排程 | 定时自唤醒 + 观察注入 |
| 生命周期 | 有界：租约 / custody / deadline | 无界：单例（`server.py:2208`），R1 运行时 `pause`（`server.py:1597/1607`） |
| 单次工具执行 | `loop_tool_execution._execute_with_timeout:973` | 同左（**共用**） |

### 1.2 `task_type` 是**现存的**认知面轴

`task_type` 是 `run_llm_loop` 的形参（`loop.py:6094`），由 `agent.py:1105` 从 `task["type"]` 写入 `self._current_task_type`，再经 `ctx.task_type` 流到工具层。

**已存在的面**（不完整，因为无枚举）：

| 面 | 证据 |
|---|---|
| `task`（默认/隐式） | `agent.py:233` 的 `str(task.get("type") or "task")` |
| `evolution` | `supervisor/evolution_lifecycle.py:212` 造 `{"type": "evolution"}` |
| `review` | `config.py:540`；`gateway/tasks.py:467` |
| `deep_self_review` | `config.py:543`；`agent.py:1154` |
| `scope_review` **与** `scope-review` | `config.py:546` —— **同一面两种拼写并存** |
| `consciousness` | `config.py:545`；`loop_llm_call.py:1477` |
| `summarize` | `loop_llm_call.py:1477` |

**每面策略已经存在，但散落在至少 8 处**：

| # | 位置 | 该处编码的 per-surface 策略 |
|---|---|---|
| 1 | `config.py:534 resolve_effort` | 推理力度（evolution/review/deep_self_review/scope_review/consciousness → `high`，其余 `medium`） |
| 2 | `context.py:234` | `{evolution, deep_self_review, review}` 的分支 |
| 3 | `context.py:1463` | backlog digest 只对 `{evolution, deep_self_review}` 注入 |
| 4 | `gateway/tasks.py:467` | API 面的面集合 `{evolution, review, deep_self_review}` |
| 5 | `loop_llm_call.py:1477` | 遥测/记账 category `("evolution","consciousness","review","summarize")` |
| 6 | `post_task_evolution.py:36` | `_SKIP_TYPES = {evolution, deep_self_review}` |
| 7 | `reflection.py:183` | 同上集合，独立写一遍 |
| 8 | `tools/git.py:2720/2761/2879`、`tools/control.py:662/698/768` | **工具按面改行为**：`if ctx.current_task_type == "evolution"` |

**没有枚举、没有校验器。** `resolve_effort` 的 `else` 对任何未知值静默退回 `medium`；两个拼写（`scope_review` / `scope-review`）各自命名，说明没有任何地方在多值上做一致性检查。

**面的工具可见性可以到「无」**：`deep_self_review` **没有工具循环**——`deep_self_review.py:501` 传 `tools=None`，`:264` 的提示自己写着「this surface has no tool loop」。所以「工具可见性」是面属性，取值范围是 全量 → 白名单 → **无**。

### 1.3 `delegation_role` 是 host 持有的血缘轴

- 活在 `task_contract.lineage`（`contracts/task_contract.py:560`，默认 `"root"`）。
- 自陈：`tools/control_delegation.py:838`「lives on the task metadata / contract lineage, **NOT as a ToolContext attribute**」。
- host 持有、防伪造：`cli.py:187-188`、`gateway/tasks.py:487-488` 对外部面拒绝任何非 `root`；`cli.py:213-214` 把 host-owned 键排在展开最后。
- 约 **150 处**消费者，横跨 `ouroboros/` 与 `supervisor/`。
- 取值 `root` / `subagent` / `background`（`tool_capabilities.py:8`）；**无取值校验器**。
- **单一来源已被测试钉住**：

  ```python
  # tests/test_owner_live_delivery.py:93-102
  def test_consciousness_stamps_the_shared_background_role(self):
      # Literal-drift pin: the producer (consciousness) and the gate
      # (owner_delivery) must share ONE constant, not two literals.
      src = inspect.getsource(consciousness)
      assert "BACKGROUND_DELEGATION_ROLE" in src
      assert '"delegation_role": "background"' not in src
  ```

### 1.4 `sender_identity` 是呈现轴（**不动**）

`"agent"`（`tools/control.py:2231` 预设）/ `"background"`（`tools/owner_delivery.py:77` 强制），消费者 `gateway/history.py:859`。它决定 UI 怎么标消息，不决定谁能做什么。

## 2. 关键发现：后台意识站错了轴

| 现象 | 证据 |
|---|---|
| 实际的后台循环盖的是**血缘轴**的值 | `consciousness.py:1290` 写 `delegation_role="background"` |
| 而**认知面轴上也有它的值** | `config.py:545` 与 `loop_llm_call.py:1477` 都有 `"consciousness"` |
| 于是 BG 在两条轴上各占一半 | 力度/遥测按面读它，投递/闸门按血缘读它 |

**代价——`tools/core.py` 同一函数里两轴各判一次，正确性靠顺序维持：**

```
:2397  if delegation_role == BACKGROUND_DELEGATION_ROLE:   # 血缘轴读（实为面语义）
           return ESCALATE_UNAVAILABLE (…has no owner-interactive loop and no parent)
:2416  delegation_role = str(meta.get("delegation_role") or "").strip()
:2417  if delegation_role and not parent_task_id:          # 血缘轴读（真血缘语义）
           # Fail-closed on corrupted lineage: a delegated context without
           # its parent id must never fall through to the OWNER card path
```

`background` 恰好是**非空 `delegation_role` + 无 `parent_task_id`**——在真血缘语义眼里这就是**血缘损坏**。今天没被误伤，只因为 `:2397` 那条实为面语义的检查**先**命中。把两个不相干的维度压进一个字段，代价就是这样一条靠顺序维持的正确性。

**第二处代价——BG 必须伪造 task 身份才能复用 R1 的工具**：`consciousness.py:1290` 往 `_registry._ctx.task_metadata` 写 `root_task_id="bg-consciousness"` / `session_id="background-consciousness"` / `actor_id="background-consciousness"` / `delegation_role="background"`，这些在 BG 语境里**没有任何真实血缘**。

**第三处代价——新增面要改血缘字段。** 但注意：`evolution` / `review` / `deep_self_review` **没有**这么做，它们正确地只用 `task_type`。也就是说**本仓已经有正确的做法，只有 BG 是例外。**

## 3. 设计：把已有的面轴收敛成声明，把 BG 迁回它

**不做的事**：不新造一条与 `task_type` 平行的轴（那会变成第三套并行词汇）。轴 A（`delegation_role`）与轴 C（`sender_identity`）**一处不改**。

**做的事**：把 `task_type` 这条已有轴的散落策略收敛成**一处声明**：

```
CognitionSurface {
  id             : "task" | "evolution" | "review" | "deep_self_review"
                 | "scope_review" | "consciousness" | "summarize" | …
  effort         : 推理力度（当前散在 config.resolve_effort）
  tool_visibility: 全量 / 白名单集合 / 无     ← deep_self_review 证明「无」是合法取值
  context        : 上下文面与额外段（当前散在 context.py:234/1463）
  dispatch       : 调度策略（agent.py:233 已按面分派）
  telemetry      : 记账 category（当前散在 loop_llm_call.py:1477）
  post_processing: 任务后是否跳过某几步（当前散在 post_task_evolution.py / reflection.py）
  gates          : 工具级闸门（当前散在 tools/git.py、tools/control.py）
  delivery       : 投递模式（当前由血缘轴上的 background 兼职）
}
```

**迁移映射**：

| 现由 `delegation_role` 承担 | 归属 | 处理 |
|---|---|---|
| 投递模式（`owner_delivery.py:67-77` 的 `_deferred()`） | 面 | → `surface.delivery`；BG 改报 `task_type="consciousness"` |
| escalate 拒绝（`core.py:2397`） | 面 | → `surface.gates` |
| `sender_identity="background"`（`owner_delivery.py:77`） | **呈现轴** | **不动** |
| `delegation_role="background"` 这个**值** | —— | **退场**（BG 不再占用血缘轴） |

**收益**：

1. `core.py` 那条靠顺序维持的正确性消失——两轴不再互相误读。
2. `scope_review` / `scope-review` 的双拼写被迫统一（一处声明只能有一个 id）。
3. 未知面不再静默退回 `medium`——声明表是封闭的。
4. 新增面 = 一条声明，而不是第 9、第 10 处散落的 set 字面量。
5. BG 不再需要伪造 task 身份（等第 1 步把消费点全迁走后）。

## 4. 不变量（不可违反）

1. **能力上限是结构性的，不是 prompt 级的。** 需要更少工具的面拿到**更小的注册表**（`deep_self_review` 拿到的是**空**），绝不拿到全量注册表 + 指令。
2. **轴 A 与轴 C 不动**：`delegation_role` 的 host 持有、防伪造、`subagent` 只能经内部工具产生（`cli.py:187-188`、`gateway/tasks.py:487-488`），以及 `sender_identity` 的呈现语义，一律保持原样。
3. **已正确使用面轴的三个面（`evolution` / `review` / `deep_self_review`）行为不变**——本设计是给它们补上声明，不是改它们。
4. **R2 是 P0 的实现，不是 R1 的一种模式。** 不因「共用代码更省」而把 R2 变成 R1 的一次任务。
5. **面的失败互相隔离**：BG 异常不得影响任务执行，反之亦然。
6. **单一来源**：`tests/test_owner_live_delivery.py:93-102` 已用 `inspect.getsource` 钉住「角色值只有一个定义」。新声明必须遵守同一条，并让该 pin 覆盖它。

## 5. 共享基底（必须共用，单一实现）

| 基底 | 位置 |
|---|---|
| LLM 客户端与 wire 契约 | `llm.py`（provider 路由、物理发送台账） |
| 工具目录 | `tools/*` + `ToolRegistry` / `ToolEntry`（各建**实例**，同一目录） |
| 单次工具执行 | `loop_tool_execution._execute_with_timeout:973` |
| 上下文捕获原语 | `context.py` 的 section builders（已按 surface/partition 参数化） |
| 记忆访问 | Engram + 三个受保护文件（见主规格 §3.4） |
| 可观测性 | live log / progress / telemetry |

## 6. 面专有（必须不共用）

| 专有项 | 任务面（R1） | 后台意识（R2） | deep_self_review |
|---|---|---|---|
| 工具可见性 | 全量 | 白名单 | **无** |
| dispatch 策略 | 并行判定 + 截断 + 原文 custody + `llm_trace` | 周期收据 | 单次调用 |
| 上下文面 | 随 mode / task 变化 | `partition="all"` | 只读包 + 无工具提示 |
| 触发与生命周期 | queue / lease / deadline | timer / inbox / singleton / pause | on-demand |
| 权威闸门 | 预算 / deadline / 验收 | identity 完整度 + **预算**（`consciousness._check_budget:675-691` 读 `usage_projection`，是 R2 **唯一**的自动停止条件；主规格 §6.5 记录它将随计费删除而改形） | —— |
| 投递模式 | 常规 | cycle-end 延迟 | 无投递 |

`sender_identity`（轴 C）**不在此表**——它不是面专有项：两个面都可能产出 `agent` 呈现，BG 只是**强制**覆盖成 `background`。

## 7. 明确不合并的一项：两个收件箱

| | `owner_mailbox.py` | `consciousness` 观察收件箱 |
|---|---|---|
| 键 | **task-scoped**（`_mailbox_path(drive_root, task_id)`） | **全局**（`state/consciousness_observations.jsonl`） |
| 条目种类 | 6 种（owner_text / task_message / finalize_now / hurry / quiz_answer / control_revoked） | `{op: enqueue\|ack}` |
| 生命周期 | 任务终止即 `cleanup_task_mailbox` | 持久，跨任务 |
| 读者 | `loop.py:2490` 每轮 drain | R2 的唤醒循环 |

键、种类、生命周期都不同。相似只在「append-only jsonl + ack」这一层机制。**合并是硬凑。**

## 8. 提议的改法（按依赖顺序）

1. **把 `task_type` 的散落策略收敛成 `CognitionSurface` 声明表**（§3 的 8 处来源逐一对齐）。轴 A / 轴 C 不动。**行为不变，可独立验证**：每个面的 effort、telemetry category、post-processing skip、工具闸门在改造前后逐项对比。
2. **统一 `scope_review` / `scope-review` 双拼写**，并给未知面一个显式的失败或显式默认（不再静默 `medium`）。
3. **把 BG 迁到面轴**：`consciousness` 作为面注册（`config.py` / `loop_llm_call.py` 里已有该值），停用 `delegation_role="background"` 这个新值。
4. **收缩 `consciousness._execute_tool`**：保留白名单、identity 闸门、周期收据；把 ctx 绑定与执行交给共享原语。前置条件见 §9。
5. **让 BG 停止伪造 task 身份**——依赖第 3 步完成。

## 9. 待验证项（实施前必须实测）

1. **`task_type` 的完整取值清单。** 上面列出的 7 个面是从 8 处来源拼出的**下界**，不是全集（无枚举）。必须先做一次全仓穷举，并确认 `gateway/tasks.py` 的 API 面是否接受任意 `type`。
2. **8 处 per-surface 策略的分类。** 逐处判定：哪些是真面策略（收敛进声明）、哪些是巧合的同集合（保持分散）、哪些是同一集合被重复写了多遍（去重）。`post_task_evolution.py:36` 与 `reflection.py:183` 的同一集合 `{evolution, deep_self_review}` 是显然的去重候选。
3. **`delegation_role="background"` 的完整消费者集。** 除了已确认的 3 处（`consciousness.py:1290` 产出、`core.py:2397`、`owner_delivery.py:67`），需用 `lsp references` 确认没有第四处，并确认没有「非 `subagent` 即 `root`」的假设已把 BG 当根任务处理过。
4. **R2 能否直接调 `_execute_with_timeout`。** 需查清该原语及其下游（结果封装、任务状态投影、投递判定）读 `tools._ctx` 的哪些字段；BG 目前绑定的四个字段是被真实读取，还是只是为了让断言不炸。
5. **两个收件箱的机制是否真同形**（锁域、ack 水位、并发写假设）。
6. **R2 的 `pause` 与 R1 的租约是否有竞态**（`server.py:1597/1607`）。

## 10. 与主规格的关系

- 主规格**不动** `consciousness`（§5.13 / §12.2）。本文是它的独立规格。
- 主规格的记忆改造（Engram）会同时触及多个面：任务面经 `context.build_llm_messages`，BG 经 `_build_context`。改造后两者的记忆来源必须一致（同一 Engram 通道，同一 `scope` 语义）。
- 主规格 §5.11 的 tier-0 降级契约**对 BG 同样适用**：Engram 缺失时 BG 的上下文也必须显示显式缺口，而不是静默为空。
- **顺序**：主规格先落地（记忆外置会改动 BG 的上下文来源），再做本文的面轴收敛，避免两次改动叠在同一条上下文路径上。
