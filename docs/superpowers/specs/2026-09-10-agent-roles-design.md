# Ouroboros 两个 agent 职责的设计

- 日期：2026-09-10
- 分支：`design/slim-down-engram`
- 状态：待复核
- 与主规格的关系：**独立规格**。主规格（`2026-09-10-ouroboros-slim-down-design.md`）§12.2 明确「`consciousness` 的任何改动另开一份规格」；本文是那一份。

## 0. 一句话

本仓有两个 agent 职责（任务 agent、后台意识）与三个**权威类**（`root` / `subagent` / `background`）。设计问题不是「怎么把两者合并或分开」，而是：**`background` 目前被塞进了血缘字段，而它属于另一个维度。** 本设计主张把两个轴拆开。

## 1. 现状

### 1.1 两个职责（实测）

| 维度 | 任务 agent | 后台意识 |
|---|---|---|
| 目的 | 把 owner / 队列驱动的目标做到可交付 | 任务之间持续思考，按自身主动性行动 |
| 权威来源 | task contract / owner 委派 | P0 agency（`BIBLE.md:42-45` 具名实现） |
| 循环 | `agent.py:1217 run_llm_loop` → `loop.py:6087` | 自有 `_loop:627` / `_think:694` / `_think_scoped:714`，**不调** `run_llm_loop` |
| 工具可见性 | 全量注册表 | **独立 `ToolRegistry` 实例**（`consciousness.py:1203`）+ `_BG_TOOL_WHITELIST`（`:1193`）在 `_execute_tool:1244` **强制** |
| 上下文 | `context.build_llm_messages(mode, task)` | 自有 `_build_context`（`partition="all"`） |
| 触发 | 队列准入 / owner 消息 / 排程 | 定时自唤醒 + 观察注入 |
| 生命周期 | 有界：租约 / custody / deadline | 无界：单例（`server.py:2208`），R1 运行时 `pause`（`server.py:1597/1607`） |
| 单次工具执行 | `loop_tool_execution._execute_with_timeout:973` | 同左（**共用**） |

`context_layout.py:6-7` 另declare了第三个认知面：deep self-review。

### 1.2 `delegation_role` 是 host 持有的血缘字段，不是角色标签

这是本设计最重要的事实，也是**我上一稿写错的地方**——我曾把它当成「一个裸字符串，语义分散在三处 `if`」。实际上：

- **活在血缘上**：`contracts/task_contract.py:560` 在 `lineage` 里归一化它，默认 `"root"`。
- **自陈**：`tools/control_delegation.py:838` —— 「delegation_role lives on the task metadata / contract lineage, **NOT as a ToolContext attribute** — read it the canonical way.」
- **host 持有、防伪造**：`cli.py:187-188` 与 `gateway/tasks.py:488` 都拒绝外部传入非 `root` 值（「only allowed through the internal schedule_subagent tool」）；`cli.py:213-214` 把 host-owned 键**排在展开最后**，使 `--task-metadata-json` 无法伪造。
- **规模**：全仓 `grep -rn delegation_role` 命中约 **150 处**，横跨 `ouroboros/` 与 `supervisor/`（`supervisor/events.py` 单文件就有数十处 `== "subagent"` 判定）。

取值：`"root"`（默认）| `"subagent"` | `"background"`（`tool_capabilities.py:8`）。

**取值没有类型级校验器**——`grep` 全仓找不到 `VALID_DELEGATION_ROLES` 之类的枚举，拒绝只发生在**表面**（`cli.py:187-188`、`gateway/tasks.py:487-488` 对外部面拒绝任何非 `root` 值）。

**但「单一来源」已经被测试钉住**，这是本设计的重要支持性证据：

```python
# tests/test_owner_live_delivery.py:93-102
def test_consciousness_stamps_the_shared_background_role(self):
    # Literal-drift pin: the producer (consciousness) and the gate
    # (owner_delivery) must share ONE constant, not two literals.
    import inspect
    from ouroboros import consciousness
    src = inspect.getsource(consciousness)
    assert "BACKGROUND_DELEGATION_ROLE" in src
    assert '"delegation_role": "background"' not in src
```

即：本仓**已经**用源码文本断言强制「角色值只能有一个定义、消费方引用它而非复制字面量」。轴 B 的设计正是把这条既有纪律从「一个值」扩展到「一整组属性」。

### 1.3 第三个轴：`sender_identity`（呈现层，**不动**）

| 轴 | 字段 | 取值 | 消费点 |
|---|---|---|---|
| **C：呈现** | `sender_identity` | `"agent"`（`tools/control.py:2231` 预设）/ `"background"`（`tools/owner_delivery.py:77` 强制） | `gateway/history.py:859`（渲染用） |

它决定 UI 怎么标这条消息，不决定谁能做什么。**本设计不动它。**

## 2. 关键发现：两个轴被压进一个字段

| 轴 | 含义 | 取值 | 本质量 |
|---|---|---|---|
| **A：血缘 / 授权** | 这份工作属于谁、继承谁的 scope、能否建子任务、预算算谁的树、能否独立取消 | `root` / `subagent` | **委派**概念 |
| **B：认知面 / agent 身份** | 跑哪个循环、工具上限、上下文 partition、由谁唤醒、投递模式 | 任务 agent / 后台意识 | **agent 身份**概念 |

`background` 是**轴 B 的值被写进了轴 A 的字段**。证据链：

1. **BG 没有血缘。** 它不建 task、无 `parent_task_id`、不参与任务树、不经队列准入、无委派预算。它是单例 + 自有触发。
2. **但消费点按轴 A 读它。** `tools/owner_delivery.py:67-77` 用同一个字段做**两件不同的事**——强制 `sender_identity="background"`（轴 C，呈现，**保留**）并返回 `_deferred()`（轴 B，投递模式，**应迁走**）。`tools/core.py:2397` 用同一个字段拒绝 `escalate`（「background cognition has no owner-interactive loop and no parent」——**这句注释自己就承认了它是「无父」的血缘异常**）。
3. **`tools/core.py` 同一函数里两轴各判一次，且依赖检查顺序才没出事。**

   ```
   :2397  if delegation_role == BACKGROUND_DELEGATION_ROLE:      # 轴 B 读
              return ESCALATE_UNAVAILABLE (background cognition has no … parent)
   :2416  delegation_role = str(meta.get("delegation_role") or "").strip()
   :2417  if delegation_role and not parent_task_id:             # 轴 A 读
              # Fail-closed on corrupted lineage: a delegated context without
              # its parent id must never fall through to the OWNER card path
              return ESCALATE_UNAVAILABLE (delegated context without a parent)
   ```

   `background` 的正是一个**非空 `delegation_role` 且没有 `parent_task_id`** 的组合——在轴 A 眼里这读起来就是**血缘损坏**。今天没被误伤，只因为 `:2397` 的轴 B 检查**先**命中。把两个不相干的语义压进一个字段，代价就是这样一条靠顺序维持的正确性。
4. **BG 为了复用 R1 的工具，必须伪造一个 task 形状的身份。** `consciousness.py:1290` 往 `_registry._ctx.task_metadata` 写 `root_task_id="bg-consciousness"` / `session_id="background-consciousness"` / `actor_id="background-consciousness"` / `delegation_role="background"`——**这些字段在 BG 语境里没有任何真实血缘**，存在的唯一理由是 R1 侧工具（结果封装、任务状态、投递判定）会读它们。

## 3. 为什么这个混淆有代价

1. **新增认知面要改血缘字段。** deep self-review 已是第三个认知面（`context_layout.py:6-7`），但它没有 `delegation_role` 值。要给它同等对待，就得再往轴 A 加一个值——而轴 A 的消费者是「像 `subagent` 吗」这类血缘判定的 150 处代码。
2. **血缘语义被污染。** `"background"` 出现在一个默认值为 `"root"`、且被防伪造成 `root`/`subagent` 的字段里，意味着任何「非 `subagent` 即 `root`」的既有假设都可能静默把 BG 当根任务处理。
3. **BG 与 R1 的耦合点落在 `_registry._ctx` 的原地改写上。** 这是隐式契约：工具实现假设调用者有一份 task 身份。

## 4. 设计：两轴分离

**轴 A（血缘/授权）保留原样**：`root` / `subagent`，host 持有、防伪造、150 处消费者的语义一律不动。这是本仓最成熟的安全边界之一，本设计**不碰**。

**轴 B（认知面）独立成一处声明**：

```
CognitionSurface {
  id             : "task" | "background" | "deep_self_review" | …
  authority      : 行动依据（task contract / P0 / review mandate）
  tool_visibility: 注册表构造方式（全量 / 白名单集合）
  dispatch       : 调度策略（并行判定、超时来源）
  context        : 上下文面与 partition
  trigger        : 唤醒来源
  lifetime       : 有界 / 无界；是否随 R1 暂停
  gates          : 执行前闸门（identity 完整度、escalate 可用性）
  delivery       : 投递模式（常规 / cycle-end 延迟）
}
```

| 现由 `delegation_role` 承担 | 归属 | 处理 |
|---|---|---|
| `owner_delivery.py:67-77` 的 `_deferred()`（cycle-end 延迟） | 轴 B | → `surface.delivery` |
| `owner_delivery.py:77` 的 `sender_identity="background"` | **轴 C（呈现）** | **不动** |
| `core.py:2397` 的 escalate 拒绝 | 轴 B | → `surface.gates` |
| `consciousness.py` 的白名单与 partition | 轴 B | → `surface.tool_visibility` / `surface.context` |

轴 A 的判定（`== "subagent"`）**一处不改**。

**收益**：第三个认知面 = 一条新声明，不是给血缘字段加值、也不是再散一组 `if`。

**实现约束（沿用既有纪律，不新发明）**：`tests/test_owner_live_delivery.py:93-102` 已经用 `inspect.getsource` 钉住「角色值只有一个定义、消费方引用常量而非复制字面量」。轴 B 的每个 surface 值必须遵守同一条，并让该测试覆盖新声明——否则这次重构会亲手把自己想消除的问题重新引进来。

## 5. 不变量（不可违反）

1. **能力上限是结构性的，不是 prompt 级的。** 需要更少工具的角色拿到**更小的注册表**，绝不拿到全量注册表 + 指令。
2. **轴 A 与轴 C 不动**：`delegation_role` 的 host 持有、防伪造、`subagent` 只能经内部工具产生（`cli.py:187-188`、`gateway/tasks.py:487-488`），以及 `sender_identity` 的呈现语义（`agent` / `background`），一律保持原样。
3. **R2 是 P0 的实现，不是 R1 的一种模式。** 不因「共用代码更省」而把 R2 变成 R1 的一次任务。
4. **角色失败互相隔离**：R2 异常不得影响 R1 的任务执行，反之亦然。
5. **identity 写入路径保持单一**，两个角色经同一工具，R2 额外承担完整度闸门。

## 6. 共享基底（必须共用，单一实现）

这些是**能力**不是**权限**，共用不削弱边界：

| 基底 | 位置 |
|---|---|
| LLM 客户端与 wire 契约 | `llm.py`（provider 路由、物理发送台账） |
| 工具目录 | `tools/*` + `ToolRegistry` / `ToolEntry`（各建**实例**，同一目录） |
| 单次工具执行 | `loop_tool_execution._execute_with_timeout:973` |
| 上下文捕获原语 | `context.py` 的 section builders（已按 surface/partition 参数化） |
| 记忆访问 | Engram + 三个受保护文件（见主规格 §3.4） |
| 可观测性 | live log / progress / telemetry |

## 7. 角色专有（必须不共用）

| 专有项 | R1 | R2 |
|---|---|---|
| 工具可见性 | 全量 | `_BG_TOOL_WHITELIST`（**执行期检查**） |
| dispatch 策略 | 并行判定 + 截断 + 原文 custody + `llm_trace` | 周期收据 |
| 上下文面 | 随 mode / task 变化 | `partition="all"` |
| 触发与生命周期 | queue / lease / deadline | timer / inbox / singleton / pause |
| 权威闸门 | 预算 / deadline / 验收 | identity 完整度 |
| 投递模式 | 常规 | cycle-end 延迟 |

`sender_identity`（轴 C）**不在此表**——它不是角色专有项：两个角色都可能产出 `agent` 呈现，BG 只是**强制**覆盖成 `background`（`owner_delivery.py:77`）。它保持现状，不并入 surface 声明。

## 8. 明确不合并的一项：两个收件箱

| | `owner_mailbox.py` | `consciousness` 观察收件箱 |
|---|---|---|
| 键 | **task-scoped**（`_mailbox_path(drive_root, task_id)`） | **全局**（`state/consciousness_observations.jsonl`） |
| 条目种类 | 6 种（owner_text / task_message / finalize_now / hurry / quiz_answer / control_revoked） | `{op: enqueue\|ack}` |
| 生命周期 | 任务终止即 `cleanup_task_mailbox` | 持久，跨任务 |
| 读者 | `loop.py:2490` 每轮 drain | R2 的唤醒循环 |

键、种类、生命周期都不同。相似只在「append-only jsonl + ack」这一层机制。**合并是硬凑。**

## 9. 提议的改法（按依赖顺序）

1. **引入轴 B 的声明（`CognitionSurface`）**，把投递模式、escalate 闸门、白名单、partition 从 `delegation_role` 的读取中移出。轴 A 与轴 C 一处不改。**行为不变，可独立验证。** 每个 surface 值只定义一次，并扩展 `tests/test_owner_live_delivery.py:93-102` 的 `inspect.getsource` pin 覆盖新声明（§4 的实现约束）。
2. **把 deep self-review 登记为第三个 surface**，消除「面存在但无声明」的不一致。
3. **收缩 `consciousness._execute_tool`**：保留白名单、identity 闸门、周期收据；把 ctx 绑定与执行交给共享原语。前置条件见 §10。
4. **让 BG 不再伪造 task 身份**——依赖第 1 步把「工具读 `delegation_role`」的消费点全部改到轴 B 之后才可能。

## 10. 待验证项（实施前必须实测）

1. **R2 能否直接调 `_execute_with_timeout`。** 需查清该原语及其下游（结果封装、任务状态投影、投递判定）读 `tools._ctx` 的哪些字段。BG 目前绑定的 `root_task_id`/`session_id`/`actor_id`/`delegation_role` 是否被真实读取，还是只是为了让断言不炸。
2. **`delegation_role` 的完整消费者分类。** 150 处需按轴分类：哪些是轴 A 血缘判定（不动）、哪些实际是轴 B 认知面判定（迁走）、哪些两轴混用（需拆）。**这是本设计最大的一块工作量，且必须先做完分类才能动第 1 步。**
3. **两个收件箱的机制是否真同形**（锁域、ack 水位、并发写假设）。若不同形则 §8 的「可抽薄 helper」也取消。
4. **R2 的 `pause` 与 R1 的租约是否有竞态**（`server.py:1597/1607`）。
5. **`"background"` 是否已被某些「非 `subagent` 即 `root`」的假设静默当根任务处理过。** 需在 150 处里筛出这类假设，评估历史行为影响。

## 11. 与主规格的关系

- 主规格**不动** `consciousness`（§5.13 / §12.2）。本文是它的独立规格。
- 主规格的记忆改造（Engram）会同时触及两个角色：R1 经 `context.build_llm_messages`，R2 经 `_build_context`。改造后两者的记忆来源必须一致（同一 Engram 通道，同一 `scope` 语义）。
- 主规格 §5.11 的 tier-0 降级契约**对 R2 同样适用**：Engram 缺失时 R2 的上下文也必须显示显式缺口，而不是静默为空。
- **顺序**：主规格先落地（记忆外置会改动 R2 的上下文来源），再做本文的轴分离，避免两次改动叠在同一条上下文路径上。
