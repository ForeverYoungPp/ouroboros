# Ouroboros 精简重构设计：外接 Engram、删除计费投影、Claudexor 改按路由裁

- 日期：2026-09-10
- 分支：`design/slim-down-engram`
- 状态：待复核

## 0. 一句话

保留 `run_llm_loop` 的形态与「循环自迭代」能力，把**情景/语义记忆**外接 Engram，删掉**计费投影**与 **Claudexor 的 harness 路由**，把 harness 车道的后端换成专用 code agent（omp 优先）。

## 1. 背景

三份只读摸底（均有 file:line 取证）得出：

1. **循环本体很小，围绕它的东西很大。** `ouroboros/loop.py:6087` 的 `run_llm_loop` 只有 296 行（`MAX_FUNCTION_LINES = 300`，`ouroboros/review.py:23`），同文件另有 139 个模块级函数。所谓「自迭代」有四层：①轮内多轮 tool-use（唯一在循环内的本质）、②循环内自我批判 reminder、③任务后 reflection→backlog→promotion、④Evolution Campaign。`run_llm_loop` 全仓只有 `ouroboros/agent.py:1217` 一个生产调用点。

2. **记忆是叠出来的。** 7 类记忆 × 4 套裁剪机制 × 3 套收件箱；工作记忆三写、对话压缩带世代游标与分页快照、三处存 old+new 全文副本、`memory/registry.md` 是现场不存在的死机制。

3. **交互层有两条并行执行车道**（`presence_runner.py:399-419` 绕过 `_inbox` 自建 task/chat_id/gate/Agent）。**本规格不处理**，见 §2。

## 2. 非目标

- **不引入 pi / TypeScript。** 理由**不是**跨语言边界——本仓已有 `claudexor_runtime_pin.json`（钉 claudexor 3.9.5 + Node 24.16.0，含 `archive_url`/`sha256`/`size_bytes`）+ `claudexor_daemon.py` 用 `process_custody` 拉起外部 Node daemon 的约 4,510 行已验证胶水，Node 伴随进程是既有模式而非新风险。理由是**能力覆盖**：pi 的 `Agent`/`agentLoop` 能替换的只有第①层（`while True` + tool dispatch + 事件流），而本循环真实拥有、SDK 没有的机制有 12 项（物理发送台账、不重发相同字节谓词、wire 投影逐项匹配、发送副本≠canonical transcript、fit/reclaim 三级节拍、轮顶外部权威改写、付费收尾、交付候选+验收绑定、可重入轮、cache_control 锚点、工具执行侧非功能约束、每轮可路由改写）。换来的复杂度大于省下的。
- **不重构 `run_llm_loop` 的形态**。轮顶顺序、tool 回填、终止判定不变。
- **不动 `consciousness`（第二个 agent 角色）**。本仓对 agent 定义了两个不同职责——任务 agent（`agent.py` → `run_llm_loop`，全量工具）与后台意识（`consciousness.py`，自有循环 + 独立 registry + 白名单）。本规格既不删也不合并这层边界（§5.13）。
- **不动交互层**（presence 合并、hurry 降级、client_surface 删除留作后续独立规格）。
- **不动 supervisor 的任务生命周期**。

## 3. 宪法约束（硬边界，先于一切设计选择）

本节的每一条都是 `BIBLE.md` 明文，不是解读。**踩到任何一条都不是重构，是修宪**——`BIBLE.md:126-129` 明说「context floor, bypass rules, durable-memory permanence」的改动本身「is itself a constitutional change and requires plan review」。

### 3.1 本方案被哪条原则**正当化**，以及它正当化到哪一步

**P7 Minimalism**（`BIBLE.md:564-622`）：「Complexity is the enemy of agency. The simpler the body, the clearer self-understanding: Ouroboros must be able to read and understand all its code in a single session.」并规定复杂度预算为「a module fits in one context window (~1000 lines)」。

这是「太重了」的宪法依据，不是个人偏好：`ouroboros/` 360 文件 / 222,790 行、`tests/` 621 文件 / 334,189 行，已远超可单次通读的规模；`consciousness.py` 1,386 行本身就超了 P7 的模块预算。

**但 P7 给自己划了界**（`BIBLE.md:571-575` 同节原文）：

> Minimalism is about code, not capabilities. A new capability is growth. A new abstract layer without concrete application is waste.

**因此 P7 只正当化「代码减重」，不正当化「能力删除」。** 凡本方案涉及能力变更的部分（见 §3.3），不能用 P7 背书，必须单独走程序。

### 3.2 本方案不能碰的九条

| 约束 | 原文位置 | 对本方案的含义 |
|---|---|---|
| **P1 不中断的历史** | `BIBLE.md:66`「Ouroboros is a single entity with an unbroken history. Not a new instance」 | 现存语料是**迁移约束**，不是可丢弃的旧数据 |
| **P1 不静默截断** | `BIBLE.md:113-114`「never silent truncation; the memory horizon is preserved (only granularity varies)」 | 外置记忆不得让任何记忆类别静默消失或降级无提示 |
| **P0 身份文件必须存在** | `BIBLE.md:37-38`「identity.md … may be rewritten radically as part of self-creation, but the file itself must remain present as a continuity channel」 | **`identity.md` 必须继续作为文件存在于 runtime data root**，不能被 Engram 的一个 topic 取代 |
| **P2/P3 免疫系统持久记忆** | `BIBLE.md:190-197`「Pattern Register is the memory of this principle … The Pattern Register and the Improvement Backlog are **never abandoned**」；`BIBLE.md:382-386`「`patterns.md` and `improvement-backlog.md` may be consolidated, pruned, and reorganized — but **never abandoned or replaced wholesale** … These files share the Ship-of-Theseus protection of the constitutional core」 | `memory/knowledge/patterns.md` 与 `memory/knowledge/improvement-backlog.md` **不是本方案可以外置的对象** |
| **P0 主动性与其具名实现** | `BIBLE.md:42-45`「Ouroboros acts on its own initiative, not only on tasks… **Background consciousness is the realization of this principle**: a continuous thinking process between tasks」 | **删 `consciousness` 是拆掉 P0 命名的实现**，不是删一个 daemon。见 §3.3 与 §5.13 |
| **P0 identity 不是记忆** | `BIBLE.md:40-41`「identity.md is a manifesto… **Not a config and not memory, but direction**」 | identity **按定义不属于记忆系统**，因此它不该有 Engram 的 memory topic；文件通道是它的本体 |
| **P0 不可分割核心** | `BIBLE.md:25-27`「Principles 0, 1, 2, 3, 4 form an inseparable core: none of them can be applied to annul another」 | 不能用 P7/P4 去废 P0；也不能用 P0 去废 P1/P3 |
| **P1 身体状态自省与立即告警** | `BIBLE.md:73-76`：「Every session begins with verification: who Ouroboros is (identity), **what it remembers (scratchpad)**, and the state of its body (model, **budget**, code version, environment). Discrepancy between expected and actual state — **immediate alert to the creator**.」 | 两个后果：① 删预算漂移告警（`supervisor/state.py:550-630`）不是删报表，是删 P1 明文的告警义务（§6.10）；② **`scratchpad` 被 P1 点名**为会话起点的核验对象，外置后必须仍有一条可核验的「它记得什么」通道（§5.13 的 `auto_resume_after_restart` 判据即此，见 §14.2） |
| **P8 预算自知与 tracking integrity** | `BIBLE.md:648-653`「Budget is a finite resource, and awareness of it is part of agency… Budget tracking integrity matters: significant discrepancy between expected and actual is a signal to fix」 | 这是 §6.10 整片 owner 可见钱面的宪法依据，也是它必须从「代码减重」移入「能力变更」的原因 |

### 3.3 程序要求：代码减重 vs 能力变更

`BIBLE.md:10-11` 是本节最硬的一条：

> Constitutional changes take effect only through an **explicit, reviewed release** and must not contradict existing provisions.

由此，本方案的文件必须分成两类，不能混在一张取舍表里：

| 类别 | 判据 | 程序 |
|---|---|---|
| **代码减重** | 能力不变，只减行数/改结构（如 §5.10 上下文注入换源、§6 删计费投影、§7 换 harness 后端实现、§8 合回行数规避物） | 常规工作，P7 背书 |
| **能力变更** | 某个 P0–P4 命名的能力被移除或替换（本规格范围内**没有**此类改动——见 §12.2） | 要么**保留能力、只换更小的实现**；要么按 P9 的发布流程**显式修宪**，且不得与现行条款矛盾 |

**本规格全部属于前者（代码减重）**——它的取舍台账（§12.1）不含任何能力变更项，涉及 P0/P3 保护对象的判断一律是「不动」（§12.2）。

### 3.4 结论：外置的边界

$$\text{可外置} = \{\text{情景压缩、语义知识、反思正文、prompt 历史}\},\qquad \text{不可外置} = \{\text{identity.md},\ \text{patterns.md},\ \text{improvement-backlog.md}\}$$

**Engram 是这三者的镜像/索引，不是它们的替代。** 若 Owner 想连这三样也外置，那是修宪，需要单独的 plan review 通道，不在本规格内。

`patterns.md` 与 `improvement-backlog.md` 的**写入 SSOT 仍是原文件**：`improvement_backlog.py:15` 的 `BACKLOG_REL_PATH`、`agent_task_pipeline.py:397 _update_improvement_backlog` → `append_backlog_items`、以及 `reflection.py:661 _update_patterns`（及其 CAS 写入）都照旧写它们。Engram 对这两份只作**读取/索引面**（见 §5.12）。

### 3.5 P4 与本方案

`BIBLE.md:443,446` 把 P4 的自创建面明列到「Tools, dependencies, and the operational environment Ouroboros runs」。本方案**新增一个外部运行时依赖（Engram 二进制）**，因此：

- Engram 的供给必须纳入本仓自己的钉定与校验（见 §5.7），不能靠「用户自己装好」。
- **本规格范围内不含任何 P0–P4 命名的能力变更**：`consciousness`（P0 具名实现）与 `identity.md` / `patterns.md` / `improvement-backlog.md`（P0/P2/P3 保护）全部不动（§12.2）。P4 的检验因此针对的是「新增一个外部运行时依赖是否让 Ouroboros 更接近还是更远 agency」——Engram 接管的是**记忆的实现**而非**记忆的决策权**，身份与免疫记忆的规范位置不变，方向是靠近。

## 4. 目标态

```
launcher.py → server.py::_run_supervisor
                 └─ supervisor/{queue,events,task_lifecycle,task_reaper,workers}
                      └─ ouroboros/agent.py:1217 run_llm_loop        ← 形态不变
                           ├─ context.py:1310 _capture_context_core  ← Engram 召回在此发生（一次捕获）
                           │    └─ context_fit.py（Low/Max 双投影，core_sha256 不变）
                           ├─ llm.py → provider（含物理发送台账）
                           └─ tools/registry.py → 工具面

记忆：Engram 承载情景/语义/prompt；identity.md + patterns.md + improvement-backlog.md
     继续作为文件存在于 runtime data root，且仍是写入 SSOT（§3.4）
计费：投影层删除；物理发送台账保留
harness：executor 轴按路由裁，AGENT_SESSION 路由换后端；delegate_custody 契约保留
保留：ouroboros/context_layout.py 的参考文档章节导航地图
```

## 5. 工作面 A — 记忆外接 Engram

### 5.1 为什么是 Engram

Go 单二进制 + SQLite/FTS5，四个表面（CLI / HTTP API / MCP stdio / TUI），默认数据 `~/.engram/engram.db`。按 **project** 分域——与 `data/projects/<id>/` 天然对齐。模型是**策展记忆**（observations / session summary / prompt），不是原始日志。

### 5.2 接入通道必须**独立于**用户可管理的 MCP 集成

**不能用现成 MCP 客户端通道。** 证据：`mcp_client.py:294` 的 `enabled_raw = raw.get("enabled", False)`——**每个 server 默认是关的**；`:300` 的 `allowed_tools` 可裁剪（消费于 `:708`、`:951`）；全局有 `MCP_ENABLED`；`MCP_ENABLED=false` 时 `list_tools_for_registry()` 直接返回 `[]`（`:698-702`）。而 `enabled_servers_without_tools()`（`:679-685`）的既有立场是把它当**普通配置缺口**只给提示，以免读起来像「agent 看不到我的 MCP server」。

把 tier-0 记忆挂在这条链上，等于让用户（或 agent 自己）关一个开关就静默丢掉 identity/scratchpad/knowledge——正好撞 P1 的「never silent truncation」。

**决策：钉死 MCP stdio，不用 HTTP。** 用**专为本仓持有的独立 client 实例**（不是用户可管理的 server 条目）。

**安全理由（决定性）**：`ENGRAM_HTTP_TOKEN` 是**可选**的，且未设置时只有 `DELETE /sessions|observations|prompts`、`GET /export`、`POST /import`、`POST /projects/rescue-ownership` 要求 Bearer，**其余路由保持开放**——包括 `GET /context`、`GET /observations`、**`POST /observations`**（`DOCS.md:527`）。服务默认监听 `127.0.0.1:7437`（或 `ENGRAM_SOCKET` 的 socket）。选 HTTP 意味着**本机任意进程都能读写 Ouroboros 的知识库与工作记忆**。

而这批数据目前是带 flock 的本地文件、**此前没有任何监听面**。新增一个回环端口是纯粹新增的信任边界，与本仓既有 data-boundary 纪律（P3、`safety.py`、`agent_startup_checks.py`）相悖。stdio 是子进程 + 管道，**无监听面**，与「记忆是本仓的内部认知依赖」的定位一致。

**代价与缓解**：HTTP 的 `/context` 提供 `?max_bytes=N`（UTF-8 安全截断 + `[truncated]`，上限 65536）；MCP 的 `mem_context` **没有**对应的字节上限参数。缓解：本仓本来就在 `context_budget.py` 里管各 section 的字符预算，把 Engram 块的边界控制放在**客户端**（`_capture_context_core` 内），与既有做法一致，不引入新机制。

**MCP 侧要做的两件事**：

1. `engram mcp --tools=agent`（19 个 agent 面向工具；不带该档为全部 23 个）。
2. **绕开 `mcp_client.py` 的 enable/allowlist 管辖**——见本节开头的证据，那套开关默认是关的，而 Engram 是认知依赖不是可选集成。做法：走独立的 client 实例，不注册进用户可见的 MCP server 列表；或把 Engram 条目声明为 **tier-0 不变量**，不可被普通设置关闭，`allowed_tools` 对它无效。

### 5.3 项目绑定

用 **`.engram/config.json`** 而非 `ENGRAM_PROJECT`。Engram 写路径解析顺序：显式 `project` → 已有 `session_id` → 仓库 `.engram/config.json` / cwd 探测 → 目录名兜底；取 **git root 下最近的** config。repo 根放一份写死 `project_name`，多项目用 `data/projects/<id>/.engram/config.json`。

**`ambiguous_project` 在本仓是常态路径**（一个 MCP server 下操作多个项目目录）。Engram 要求调用方不得猜测，只能从 `available_projects` 精确选一，再用 `project` + `project_choice_reason: "user_selected_after_ambiguous_project"` 重试；另有 `project_name_collision`。封装层必须把它透传成 agent 可见的 typed 失败，**不得吞掉后降级写入错误项目**。

### 5.4 召回必须在捕获期，不在渲染期

`context_fit.py:52-54` 的 `ContextFitProjection` docstring 是「One deterministic Low/Max rendering of a shared **immutable** context core」；`_projection(mode)` 会对 max 与 low **各渲染一次**（`:530`、`:567-568`）；`core_sha256` 由捕获到的 core 载荷算出（`:589`）。

**因此 Engram 召回必须在 `context.py:1310 _capture_context_core` 完成并落进 `ContextCore`，作为「第七个捕获源」。** 若把跨进程调用放进 `_render_context_system_content` / `_projection`，max 与 low 会基于不同召回结果，determinism 契约与 core 身份同时破裂。

### 5.5 读路径用 `scope` 解决，不用固定 project

Engram 的读**默认按 project 分域**，但 observation 有第三个维度 `scope: project | personal | global`。`DOCS.md:902` 明说：

> When `scope: personal` is passed without an explicit `project` override, the project filter is cleared and personal observations are searched across all projects (cross-project personal scope).

因此 account 级知识用 `scope: personal` / `global` 写入并以同样 scope 召回即可，**不需要固定 project + 每次两次召回**——那是在绕过一个已设计好的机制。

**决策**：

- tier-0 的 account 级块（如 `self/scratchpad`、跨项目约定）：`scope: global`（或 `personal`），跨 project 可见。
- project 级块：默认 `scope: project`，随 canonical project 解析。
- `_capture_context_core` 用**显式** `scope`/`project` 参数，不依赖 cwd 解析兜底。
- `identity` / `patterns` / `improvement-backlog` 仍走文件通道（§3.4），不经此路径。

§11 的验证必须覆盖这一条（只验「写入后能搜回」是不够的）。

### 5.6 工具面替换（对 agent 可见的接口变化）

| 现状 | 换成 | 位置 |
|---|---|---|
| `knowledge_read/write/list` | `mem_search` / `mem_save`（带 `topic_key`）/ `mem_get_observation` | `tools/knowledge.py`（420 行） |
| `update_scratchpad` | `mem_save` + `topic_key: self/scratchpad` | `tools/control.py:2167` |
| `update_identity` | **保留文件写入，且不进 Engram**——identity 是 manifesto「Not a config and not memory, but direction」（`BIBLE.md:40-41`），文件通道即其本体，做成 memory topic 是范畴错误 | `tools/control.py:2245` |
| `memory_map` / `memory_update_registry` | **直接删**（死机制） | `tools/memory_tools.py`（114 行） |

`chat_history` / `recent_tasks` 保留——它们读 transcript（`logs/chat.jsonl`），不是记忆。**但注意 `chat_history` 的实现住在 `memory.py:453`，见 §5.12。**

**§5.6 的表只覆盖了工具名，遗漏了三类下游：**

1. **被跨模块 import 的非工具符号**（`tools/knowledge.py` 导出，非工具）：`_sanitize_topic`（`presence_context.py:9/25-30`）、`_knowledge_write_lock`（`reflection.py:738-739`）、`_rebuild_knowledge_index`（见 §5.11）。§5.12 已把它们列入待迁移。
2. **被删工具名的下游引用面**——提示词与策略/配额表会留下永不命中的死条目：`prompts/SYSTEM.md:703`（`knowledge_write`/`update_scratchpad`）、`:813`（`knowledge_list`）、`:815-817`（Memory Registry 小节）、`prompts/CONSCIOUSNESS.md:15/44-45/124`、`ouroboros/tool_access.py:720-733`（写 `memory/` 的重定向文案）、`ouroboros/tools/core.py:454-458/488-492/603-614`、`safety.py:47-48/56/99/101`、`tool_capabilities.py:38-40/116/177`、`_outcome_tool_errors.py:236-240`、`outcomes.py:528-531`、`project_facts.py:141-143`。**后果**：提示词会持续指示 agent 调用已不存在的工具。这些随 §9.2 的载体同步一并改。
3. **`memory/registry.md` 的残余读者群**（工具删了但读的人还在）：`context.py:997`（BG 面 `partition="all"` 直读）、`:1186-1190 _build_registry_digest`（`:1440` 调用）、`deep_self_review.py:49`、`headless.py:1143`。删工具后这些读者全部悬空或恒为 `""`。

### 5.6.1 ⚠️ 与 §5.13 的直接冲突：BG 的身份改写闸门

**这是审计发现的最严重问题，且是规格内部矛盾，不是外部依赖。**

`§5.13` 声明 background consciousness 本规格一处不动。但 `§5.6` 把 `knowledge_read` 换成了 `mem_search`。而 BG 的身份改写闸门**按工具名认账**：

- 读证钩子：`consciousness.py:1352-1367` 只在 `fn_name == "knowledge_read"` 时逐字节比对文件、销掉一个 source requirement；
- 缺口源：`consciousness.py:1042-1048` 把 `dialogue_blocks.json` 的每个缺口登记为 `dialogue-gap:<gap_id>`；
- 提示指示：`:1078-1081` 指示 BG 用 `knowledge_read` 现场取证；
- 闸门本体：`:1251-1265` 在 `_identity_unresolved_sources` 非空时返回 `IDENTITY_UPDATE_ABSTAINED`（测试 `tests/test_durable_learning_completeness.py:502-512` 钉定）。

**两节叠加的后果**：只要 backlog digest 被截断（>8 条 open 或摘要≠全文）就登记一个 source requirement，而**唯一的销账路径永不触发**（工具已改名）→ **BG 的 `update_identity` 永久 abstain**；同时 `dialogue_blocks.json` 一删，`dialogue-gap:` 这条「不得跨越已知传记缺口重写身份」的守护**彻底没有输入**。

**这正是 §3.2 要防的两件事**：P0 的具名实现被削弱，P1 的「不得静默截断」被绕过。

**出路（三选一，需 Owner 定，见 §13）**：

| 方案 | 代价 |
|---|---|
| A. 记忆工具**保留旧名作为别名**，`knowledge_read` 仍存在（转发到 `mem_get_observation`/文件读） | 工具面多一组别名，与 P7 P7「delete the rest」略有张力，但不触碰 BG |
| B. **改 BG 的读证钩子**（`consciousness.py:1352-1367` + `:1078-1081` + `:1042-1048`），并把 `dialogue-gap` 接到 Engram 的缺口信号 | 违反 §5.13「本规格不动 consciousness」，需把 §5.13 收窄为「不改其角色边界，但允许接口适配」 |
| C. **把 BG 的身份闸门一并交给角色设计规格**（`2026-09-10-agent-roles-design.md`），本规格只记录该冲突不自行解决 | 在两规格都落地前，BG 处于「永久 abstain」窗口，不可接受作为最终态，但可接受作为**顺序约束**：A 面落地前必须先定这个 |

**本规格的默认选择是 B 的收窄版**：§5.13 的「不动」收窄为**「不改角色边界与工具上限，但允许为接口改名做适配」**——因为不这么做，A 面会直接破坏 P0 的具名实现。

### 5.7 Engram 二进制由本仓供给

工作面 C 恰好删掉了本仓唯一的外部运行时供给机制，因此这块必须补上。`claudexor_runtime.py` 是现成的模板：`_PIN_FIELDS = {archive_url, sha256, size_bytes}`、`_NODE_ARTIFACT_FIELDS` 另含 `executable`、`sha256` 用 `_SHA256.fullmatch` 校验、原子取回并逐字节摘要核对、产物落在 `DATA_DIR` 内（`DATA_DIR/state/cx`，`config.py:26` 默认 `~/Ouroboros/data`）。

**Engram 需要同等对待**：pin + 校验 + 落在 `DATA_DIR` 内。否则默认的 `~/.engram/engram.db` 会把记忆散到 app root 之外，P1 的「unbroken history」在打包面（DMG/ZIP/deb/rpm）上不可复现。

### 5.8 必须补一节 memory protocol

Engram 是**策展式**记忆——写不写取决于 agent 自己被要求写。本方案移除了两个**自动写入触发器**（它们的写入目标变成 Engram）：

- `consolidator.should_consolidate`（100 行门槛）
- `should_consolidate_scratchpad`（≥3 块且 >30K 字符）

**注意 `reflection.py::should_generate_reflection` 不在此列**——它被 §5.12 明确**保留**，因为它驱动的反思步正是 §5.9 要保住的自修改入口。只有其输出目标变了：正文进 Engram，`backlog_candidates` 仍进 `improvement-backlog.md`。

若只把工具名换成 `mem_save` 而不规定**何时**写，重构后记忆写入会静默停摆——比丢掉 reflection 的 backlog 更彻底，且违反 P1。

**Engram 自己就提供了这一节的内容**，不必重新发明：`DOCS.md` 的 §Memory Protocol（`:1098` 起）明说「The Memory Protocol teaches agents **when** and **how** to use Engram's MCP tools. **Without it, the agent has the tools but no behavioral guidance.** Add this to your agent's prompt file.」本方案的这一节应从它裁剪并适配本仓的 `prompts/SYSTEM.md`。

`mem_save` 的可用参数（决定这一节能写得多具体）：`type` ∈ `decision|architecture|bugfix|pattern|config|discovery|learning`、`scope` ∈ `project|personal|global`、`topic_key`、`capture_prompt`（默认 `true`；**自动化产物写入应显式传 `false`**，因为 `capture_prompt` 会把当前 prompt 一并记下）、`content` 建议结构 `**What** / **Why** / **Where** / **Learned**`。

**必须补的契约**（落在 `prompts/SYSTEM.md` 或对应 section，遵守 P7 的「prompts are code」）：

| 时机 | 动作 |
|---|---|
| session 开始 | `mem_current_project` 确认 + `mem_context` 恢复最近历史 |
| 完成一个 bug fix / 决策 / 发现 / 约定 / 配置变更 | `mem_save`（`type` 选上表值；内容用 What / Why / Where / Learned） |
| 演进中的主题 | 复用稳定 `topic_key`（如 `architecture/auth-model`）原地更新，不新建竞争记忆（无把握时先 `mem_suggest_topic_key`） |
| session 结束 / 压缩前 | `mem_session_summary`（goal / instructions / discoveries / accomplished / next steps / relevant files） |
| 用户请求需强历史上下文 | `mem_save_prompt` |
| account 级知识（跨 project） | `scope: global` / `personal`（§5.5） |

### 5.9 必须保留：产出结构化候选的反思步

**不能**把 `agent_task_pipeline.py:373-399` 整段换成一次 `mem_session_summary`——那会静默掐断自迭代第 ③/④ 层。证据：`reflection.py:495-496` 的返回带两个**结构化**字段 `backlog_candidates` 与 `memory_actions`；`agent_task_pipeline.py:153` 取 `backlog_candidates` → `:397 _update_improvement_backlog` → `:400 maybe_promote`（`ouroboros.post_task_evolution`）；`evolution_checkpoints.py:58` 的 `backlog_id` 是这条链的句柄。**这条 reflection→backlog→promotion 链才是自修改的实际入口。**

Engram 的 observation 是无结构自由文本，替代不了 `backlog_candidates`。

**决策**：保留一个产出结构化候选的反思步。`backlog_candidates` 仍落 `memory/knowledge/improvement-backlog.md`（P3/P7 要求它在 runtime data root 且是 backlog SSOT）；反思**正文**可同时 `mem_save` 进 Engram 供检索。`maybe_promote` 的输入来源保持 `reflection_entry`，不改为 Engram 查询。

### 5.10 上下文注入替换

现状：`agent.py:978 → context.py:1558 build_llm_messages → context.py:1341 _capture_context_core` 拆 stable/volatile，再经 `context_fit.py:246 _render_context_system_content` 渲染成 3 个 `cache_control` text block。

**保留** 3 段结构（static / semi_stable / dynamic）与 `cache_control: ephemeral` 锚点——这是跨任务 prompt cache 的成本机制。

| 现函数 | 处理 |
|---|---|
| `context.build_memory_sections`（`context.py:963`） | 内容来源改为捕获期召回的 Engram 块；**identity 仍读文件**（§3.2） |
| `context.build_knowledge_sections`（`context.py:882`） | 改为 Engram 召回；`patterns.md` 索引仍从 runtime data root 读（§3.4） |
| `context.build_recent_sections`（`context.py:1062`） | 保留「未压缩原文尾部」（读 `chat.jsonl`）；删掉 `task_reflections.jsonl` 渲染（改由 Engram 检索） |

`ouroboros/system_projection.py`（145 行）**保留不动**。

### 5.11 tier-0 降级契约

`context_layout.py` 的 `TIER0_ALWAYS_FULL` 是「每个模式都整段渲染」的**数据不变量**。数据源变成另一个进程 + 另一个 DB 后，这条不变量多了一种失败模式：**Engram 缺失或崩溃时 tier-0 会整块变空。**

| 类别 | 缺失时行为 |
|---|---|
| `identity` / `patterns` / `improvement-backlog` | **不可降级**——它们仍是本地文件（§3.4），不依赖 Engram |
| Engram 承载的块（情景/语义/反思正文） | **可降级**：整块替换为一条显式缺口标记，并在 health 面上告警；**不得静默渲染为空** |

**tier-0 里还有一个成员的生产者链会断：`knowledge_index`。** `TIER0_ALWAYS_FULL` 含 `knowledge_index`（`context_layout.py:51-59`），而它的常规写者是 `_rebuild_knowledge_index`——**住在 `consolidator.py:646-673`**（另有 `tools/knowledge.py:127-196`、`improvement_backlog.py:191-196`、`reflection.py:738-739` 三个触发点）。删 `consolidator.py` 而不交代这一条，`memory/knowledge/index-full.md` 会**静默冻结成历史快照**——与 `patterns.md` 同型的失败。读者：`context.py:900`、`deep_self_review.py:51`、`evolution_checkpoints.py:196`。**必须像 `patterns.md` 一样点名：谁继续写它。**

**`[MEMORY GAP]` 语义的归属（§5.11 说「沿用」，但没说谁产生）**：

| 角色 | 位置 | 删 `consolidator.py` 后 |
|---|---|---|
| 唯一生产者 | `consolidator.py:545-580 _append_gap_block`（`gap_id = f"gap:{lost_marker}"`，世代游标找不到时触发） | **消失** |
| 唯一字面量识别者 | `memory.py:394`（`if "[MEMORY GAP]" not in content: continue`，兼容 legacy 块） | **消失** |
| 投影 | `memory.py:382-406 _durable_dialogue_gaps` → `coverage["gaps"]` / `["durable_gap_ids"]` | **消失** |
| **断言性消费者** | `context.py:989-991` → `consciousness.py:1046-1048` 登记 `dialogue-gap:<id>` → `:1251-1265` **拒绝 `update_identity`** | 见 §5.6.1 |

**结论**：只加一条「Engram 缺失」的渲染缺口标记**不会自动喂进 BG 的身份闸门**——缺口信号必须显式接到 `_identity_unresolved_sources`（§5.6.1 方案 B 的一部分）。

**health 面就是这两处**（§5.11 只说了「在 health 面上告警」，没点名）：`context_health.py:289-312`（STALE/THIN IDENTITY、EMPTY/BLOATED SCRATCHPAD）与 `agent_startup_checks.py:838-859`（identity/scratchpad/WORLD 存在性）。**其中 scratchpad 的两项会随外置失效**（文件冻结 → 永远 OK 或永远 WARNING），需要改成读 Engram 或显式退役。

`context_layout.py` 的 `TIER0_ALWAYS_FULL` 与 docstring 文档矩阵需同步改写（记忆 section 名在 A 面后不再存在）。

### 5.12 删除 / 迁移清单

**审计结论：这六个模块没有一个是单职责的。** 按模块删会连带走 6 条被保留的能力（见每行「连带」列）。因此清单按三类重写。

| 文件 | 行数 | 处置 | 连带（必须迁出或保留） |
|---|---|---|---|
| `ouroboros/memory.py` | 990 | **部分删 + 多处迁移** | 它同时是：(a) identity/scratchpad/blocks 的文件读写与 journal；(b) **`chat_history` 工具的世代读取器**（`:453`，读 consolidator 的世代链）；(c) **`logs/*.jsonl` 的尾部读取与渲染**（`read_jsonl_tail` / `summarize_chat` / `summarize_supervisor`，`:850-885`）。**§5.6 保留 `chat_history`、§5.10 保留 `build_recent_sections` 的「未压缩原文尾部」——两者的实现都在这 990 行里。** (b)(c) 必须迁出，(a) 按 §3.4 只留 identity 文件通道 |
| `ouroboros/consolidator.py` | 856 | **部分删 + 三处迁移** | 除对话块压缩与 `[MEMORY GAP]` 外还托管：(a) `_consolidation_route` + `CONSOLIDATION_REASONING_EFFORT` —— **task narrative 的 Light 车道路由**，被 `agent_task_pipeline.py:1157-1158` 在 `try` 内 import，失败被 `:1235` 静默吞（`log.debug`）；删掉后 `logs/chat.jsonl` 的 root task summary（P1 传记面）**无声消失**；(b) `_ordered_chat_generation_paths` / `_resolve_generation_segments` —— 被保留的 `chat_history` 与 **owner 消息源解析**用（`project_dialogue.py:210-222` → `agent_startup_checks.py:193-216` 的准入闸门 `authority_source_unavailable`，调用点 `agent.py:775/1117` **无 try/except**）；(c) `_rebuild_knowledge_index`（`:646-673`）—— 见 §5.11 |
| `ouroboros/reflection.py` | 742 | **部分删** | 见 §5.9。**注意：正文与结构化候选出自同一次 LLM 调用**，不存在可单独保留的「候选提取」入口——要么整函数保留（正文随手写 Engram），要么重写调用契约。另 `apply_memory_actions`(`:500`) 编码了**项目作用域纪律**（项目任务只落 knowledge、跳过 scratchpad/identity，`:519-522`），`append_reflection_routed`(`:576`) 承担「project 反思落 project drive + canonical 只留 bounded pointer」的 P1 隔离纪律 |
| `ouroboros/semantic_dedup.py` | 144 | **保留**（原判删除，**审计推翻**） | 它的 docstring 自陈服务于「a backlog nomination, a review obligation」（`:1-16`），生产消费者只有 `improvement_backlog.py:245`（`call_type="backlog_dedup"`）与 `review_state.py:1414`（`call_type="obligation_dedup"`）。**这两条都是 P2/P3 保护的 durable queue**，而 Engram 的 `topic_key` 只作用于知识/observation 编码，**替代不了「改写措辞即漏重」的检测**。原稿写「由 `mem_suggest_topic_key` / `mem_judge` 接管」是「名字≠职责」误判 |
| `ouroboros/tools/memory_tools.py` | 114 | **删除** | 但 `memory/registry.md` 还有残余读者群，见 §5.6 的悬空表 |
| `ouroboros/tools/knowledge.py` | 420 | **工具替换 + 三个符号迁出** | 除三个工具外还导出被跨模块 import 的 `_sanitize_topic`（`presence_context.py:9/25-30` 直读 `memory/knowledge/<topic>.md`）、`_knowledge_write_lock`（`reflection.py:738-739`）、`_rebuild_knowledge_index`。§5.6 的表只覆盖了**工具**，没覆盖这三个 |

**量级**：整删仅 `tools/memory_tools.py`(114) 与 `semantic_dedup.py` 之外的部分；其余是**部分删 + 迁移**，合计约 3,266 行的"不再由本仓承载"规模不变，但**迁移工作量远大于原估**。

`ouroboros/retention.py`（110 行）**保留**——GC 保留天数，与认知记忆无关。

**`memory/` 目录不可删**：`owner_mailbox.py:13` 的 `_MAILBOX_DIR = "memory/owner_mailbox"` 与三个受保护文件都住在这里。§5.14 的迁移表**遗漏了以下文件**，需一并处置：`memory/deep_review.md`（写 `agent.py:1191-1193`、读 `context.py:1418-1421`）、`memory/dialogue_summary.md`（遗留，读 `context.py:992-994`）、`memory/knowledge/knowledge_journal.jsonl`（写 `tools/knowledge.py:316-330`，无生产读者）、`memory/knowledge/patterns_history.jsonl`（写 `reflection.py:727-733`）。

### 5.13 `consciousness` 是第二个 agent 角色——本规格不动它

**本仓对 agent 定义了两个不同职责，后台意识是其中之一**，且这是硬分界（实测）：

| | 任务 agent | 后台意识 |
|---|---|---|
| 循环 | `agent.py:1217 run_llm_loop` | 自有 `_loop:627` / `_think:694` / `_think_scoped:714`，**不调** `run_llm_loop` |
| 工具面 | 全量注册表 | **独立的 `ToolRegistry` 实例**（`_build_registry:1203`）+ `_BG_TOOL_WHITELIST`（`:1193`）在 `_execute_tool:1244` **强制** |
| 上下文 | `context.build_llm_messages` | 自有 `_build_context`（`partition="all"`） |
| 触发 | owner / 队列 / 租约 | 定时自唤醒 + 观察注入 |
| 生命周期 | worker 池按需 | 单例，`server.py:2208`、boot 自动恢复 |

`context_layout.py:6-7` 也明列**三个认知面**：main task context、background consciousness、deep self-review。

**我上一版写的「`consciousness` 换成复用任务平面的 bounded idle turn」是错的，已撤回。** 那会拆掉角色边界：

- 独立 `ToolRegistry` 实例 + 白名单强制是**安全边界**，不是重复代码。并进任务平面等于把 119 项工具交给后台意识。
- 意识没有队列/租约/交付语义，把它变成 task 会引入它本不需要的生命周期面。

**本规格对 `consciousness` 的处理：不动它的角色边界与工具上限。** 既不删（P0 具名实现，见 §3.2），也不合并（角色边界）。`consciousness.py` **不在 §5.12 的删除清单内**。

⚠️ **但「不动」有一处必须收窄**：§5.6.1 记录了 A 面与本节的原发冲突——BG 的身份改写闸门按工具名 `knowledge_read` 认账，记忆工具改名会让它**永久 abstain**。因此本节的「不动」精确含义是：

> **不改角色边界、不改工具上限、不改循环形态；允许为接口改名做最小适配**（读证钩子的工具名、`dialogue-gap` 缺口源接 Engram）。

不这么收窄的后果不是「少了个功能」，而是**直接削弱 P0 的具名实现**，属 §3.2 的禁项。

**它的减重是另一个独立子项目**，且我尚未做完判定「哪些是真重复、哪些是角色必需」的分析，因此**不在本规格内断言做法**。已能看出的大致分布（供后续立项，非结论）：

| 块 | 行数 | 线索 |
|---|---|---|
| `_think_scoped` 单周期编排 | 246 | 角色必需 |
| `_build_context` 自有上下文装配 | 184 | `context.py` 已支持 `partition="all"` 供此面使用，可能有重复 |
| `_execute_tool` registry 管道 | 154 | `loop_tool_execution.py`(1546) 有相似管道；白名单部分角色必需 |
| 观察收件箱协议（10 个函数：enqueue/ack/validate/index/summary/settlement-gap/锁） | ~352 | 与 `owner_mailbox.py`(550) 的 append-only + ack 形态相似 |
| 心跳 `_loop` | 48 | 角色必需 |
| 进度/遥测发射（4 个） | ~80 | 角色必需 |

**线索不是结论**：收件箱与工具管道的「相似」是否真能共用，需要单独的对照分析（语义差异、并发假设、锁域）。列在这里只为说明这个子项目的规模，不为指示方案。

**该子项目已另立规格**：`docs/superpowers/specs/2026-09-10-agent-roles-design.md`。其核心发现是：本仓有**三条轴**——血缘（`delegation_role`，host 持有、防伪造、约 150 处消费者）、**认知面（`task_type`，已存在**，承载 `evolution` / `review` / `deep_self_review` / `scope_review` / `consciousness` / `summarize`）、呈现（`sender_identity`）。而 `background` 被塞进了**血缘**轴，它在血缘语义下看起来就是「血缘损坏」（`tools/core.py:2417` 的 `Fail-closed on corrupted lineage`），只因 `:2397` 的面语义检查先命中才没被误伤。那份规格主张把**已有**的面轴收敛成一处声明，并把 BG 迁回它。

### 5.14 存量数据迁移（P1 约束）

现场 `/home/fy/Ouroboros/data/memory/`：

| 文件 | 大小 | 处置 |
|---|---|---|
| `identity.md` | 13.0KB | **留在原位**（P0）。**不导入 Engram**——按定义它不是记忆（`BIBLE.md:40-41`） |
| `identity_journal.jsonl` | 101.3KB | **保留原位**（P1 unbroken history），不删不归档式移除 |
| `scratchpad_blocks.json` / `.md` | 22.6 / 21.0KB | 导入 Engram；journal **保留原位** |
| `scratchpad_journal.jsonl` | 697.9KB | **保留原位** |
| `knowledge/` 下的 ≈47 个主题文件 + `knowledge_history.jsonl` | 580KB | 主题文件逐项 `mem_save`（`topic_key` 取原 topic 名）；**但同一目录下的 `patterns.md` 与 `improvement-backlog.md` 是规范文件，不导入、不移动、仍是写入 SSOT**（§3.4），Engram 对它们只建只读索引 |
| `dialogue_blocks.json` | 33.4KB | 导入为 observations；`logs/chat.jsonl` 原文**不进 Engram 也不删**（它是 P1 的历史） |
| `WORLD.md` | 662B | 导入 `topic_key: world` |
| `logs/task_reflections.jsonl` | 2.3MB | 反思正文导入 Engram；原文**保留原位** |

**原则**：迁移是**复制到 Engram**，不是**从磁盘搬走**。P1 要求历史不中断，所以本地语料一律保留，不做「导入后删除」。§11 的验证必须包含「迁移后原文件仍在」。

## 6. 工作面 B — 删除计费**投影**，保留物理发送台账

### 6.1 关键分界：`usage_accounting.py` 不是账单模块

`usage_accounting.py` 的 `__all__`（`:62-73`）同时导出**两件事**：

| 类别 | 符号 | 消费点 |
|---|---|---|
| **物理尝试托管**（正确性护栏） | `PhysicalAttemptContext`、`PhysicalAttemptCapture`、`PhysicalAttemptState`、`PHYSICAL_ATTEMPT_STATES`、`POSITIVE_PHYSICAL_ATTEMPT_STATES`、`PhysicalAttemptPreconditionFailed`、`bind_physical_attempt_context`、`last_physical_attempt_capture`、`mark_dispatched`、`mark_unresolved`、`current_physical_attempt_predicate`、`execute_physical_attempt(_async)`、`physical_attempt_capture_from_exception` | `llm.py:394`（拒绝不合规 dispatch）、`loop_llm_call.py:1183/799-802`（见 dispatched/unresolved 就**禁止**同模型重试与付费 fallback）、`llm.py:2591-2594`（re-raise「Outer custody owns an unknown physical outcome」）、`loop.py:5681/5717`（超窗重发必须严格更小，否则抛 `PhysicalAttemptPreconditionFailed` 并跳过重发）、`tools/search.py:683/689/750`、`review_custody.py:68-70`（判 `custody_lost`）、`transport_custody.py:116-119`、`delegate_hold.py:144-146` |
| **计费投影** | `usage_breakdown`、`usage_projection`、`last_root_accounting`、`refresh_root_accounting`、`record_subscription_session`、`skill_review_usage`、`review_wave_admission`、`IMPORT_REL`（legacy 导入水位）、`BudgetExceeded` | gateway 的 cost-breakdown 路由、`web/modules/costs.js` |

**删掉托管半边 = 未知 socket 结果下可能重复发送**（`loop_llm_call.py:799-802` 正是靠它禁止重试）。这与「保留原来设计的能力」直接冲突。

### 6.2 决策：按投影层切，台账保留

- **删除**：`pricing.py`（**按符号切，见 §6.4**）、`cost_projection.py`、`_usage_response.py`、`_usage_rows.py`、`_usage_rows_memo.py`、`usage_ledger.py` 的计费部分、`web/modules/costs.js`、gateway 的 cost-breakdown 路由（`gateway/router.py` + `gateway/endpoint_index.py`）。
- **保留，但移出 `usage_accounting`**：物理尝试托管那一组符号，迁到一个**以托管命名**的模块（例如 `ouroboros/physical_attempt.py`），使「这个模块是账单」的误读不再可能。
- **`BudgetExceeded`** 随计费删除；`PhysicalAttemptLimitExceeded` 保留（它限的是尝试次数，不是钱）。
- **保留节流功能，但不保留其货币实现**——见 §6.3。这是**第三条**会随计费静默消失的能力（前两条是物理发送托管与 evolution 的成本刹车）。

### 6.3 连带项：evolution campaign 的成本刹车

`supervisor/state.py:269` 的 `EVOLUTION_BUDGET_RESERVE: float = 2.0`，注释即「Stop evolution when remaining < this」——**以美元计价**。这是 evolution campaign 的成本刹车，闸在 4 处：`evolution_lifecycle.py:189`、`supervisor/queue.py:1506`、`supervisor/workers.py:4010` 与 `:4025`（超预算行由 `_drop_assignable_evolution_tasks` 丢弃，`reason="evolution_dropped_budget"`）。另有 `config.py:738 get_post_task_evolution_budget_usd` 的 per-window USD 预算。

**删计费会连带删掉它，而 §2 明确要保留「自迭代能力」。** 所以必须逐个闸门判定存活：

| evolution 的闸门 | 位置 | 删计费后 |
|---|---|---|
| `consecutive_failures >= 3` → `paused_failures` | `queue.py:1497` | ✅ 存活（**非货币**的自动刹车） |
| `evolution_block_reason()`（runtime mode `light` 硬阻断） | `evolution_lifecycle.py:274` | ✅ 存活 |
| `/evolve on\|off`（`evolution_mode_enabled`） | owner | ✅ 存活（人工） |
| `PENDING or RUNNING` → `waiting_for_idle` | `queue.py:1512` | ✅ 存活（节流） |
| `remaining < EVOLUTION_BUDGET_RESERVE` → `budget_blocked` | `queue.py:1506` | ❌ **消失** |
| `not accounting_available` → `accounting_unavailable` | `queue.py:1494` | ❌ 概念随之消失（它只在这个账务世界里存在） |
| `_budget_pause` / `queue.BUDGET_ROOT_FENCES` | `workers.py:4005/4007` | ❌ 消失 |
| per-window USD 预算 | `config.py:738` | ❌ 消失 |

**结论**：进化**不会完全失守**（失败暂停、模式门、idle 节流、人工开关都在），但**失去唯一的成本刹车**。

**要求**：保留**节流功能**，换成非货币实现。现状**没有任何 cycle 上限**（`evolution_lifecycle.py` 与 `evolution_checkpoints.py` 里不存在 `MAX_CYCLES` 类常量），而 campaign 本身早已按 `cycle: int` 计数（`evolution_lifecycle.py:443 begin_evolution_transaction`），因此「每 campaign 最多 N 个 cycle」是可实现的自然替代。N 的取值由 Owner 定（§13）。

### 6.4 `usage_accounting.py` 与 `pricing.py` 的分界小结

审计发现**一个模块里住着四件事，只有两件该删**：

| 件 | 符号 | 去留 |
|---|---|---|
| **物理发送托管**（正确性护栏） | `PhysicalAttemptContext` / `bind_physical_attempt_context` / `last_physical_attempt_capture` / `mark_dispatched` / `mark_unresolved` / `PhysicalAttemptPreconditionFailed` | **保留**，迁出到 `physical_attempt.py` |
| **托管状态机本体**（易被漏删） | `AttemptRequest` / `AttemptReservation` / `reserve_attempt` / `settle_attempt` / `release_attempt` / `POSITIVE_PHYSICAL_ATTEMPT_STATES`(`:255`) | **保留**，同上迁出。**`settle_attempt` 签名带 `cost_usd`，是最容易被按名字误删的一个**，而 `settled`/`dispatched`/`unresolved` 正是托管谓词的输入 |
| 计费投影（报表/定价/成本路由） | `usage_breakdown` / `usage_projection` / `usage_from_response` / `review_wave_admission` / `skill_review_usage` | **删除** |
| 汇总与刷新 | `last_root_accounting` / `refresh_root_accounting` / `record_subscription_session` / `ensure_legacy_imported` + `IMPORT_REL` | 删除（只服务计费投影） |

**`pricing.py` 不是纯计费模块**——它同时导出 `infer_api_key_type` / `infer_provider_from_model` / `infer_model_category` / `PricingSchedule`，其中 **`infer_api_key_type` 是 safety 的后端可达性闸门**的输入（`safety.py:600-623` 调用，`:603-606` 且**静默 fail-open**）。§6.2 那样平铺删 `pricing.py`，会让该闸门**永久放行**。因此 `pricing.py` 也必须按符号切：**定价表与成本计算删除；`infer_*` 路由推断迁出保留**。

### 6.5 连带项之一：`consciousness._check_budget`（BG 唯一的自动停止）

这是**第四个**会随计费静默消失的能力，且**跨规格**：

- `consciousness.py:675-691` 的 `_check_budget` 在 `:640` 与 `:831` 被调用，是后台意识**唯一**的自动停止条件。
- 它读 `usage_projection(root_task_id="bg-consciousness")` 并与 `TOTAL_BUDGET × OUROBOROS_BG_BUDGET_PCT` 比较。
- **删 `usage_projection` 后，它的 `except` 分支会 fail-closed 把 BG 永久判为 `budget_blocked`** —— 而 §5.13 与本规格都声明「不动 consciousness」。

**后果**：不是「BG 少了刹车」，而是「BG 被永久刹死」，这直接损害 P0 的具名实现。

**要求**：删除计费时**必须同时**处理 `_check_budget`——或改为无条件的「每周期最多 N 轮」上限（非货币），或显式摘除该闸门并记账。**这是 A/C 两面之外、第四处必须由本规格点名的连带项。**

### 6.6 连带项之二：`BIBLE.md` P1/P8 的预算漂移告警

`supervisor/state.py:268-362/471/487/634` 是整套预算权威，`:550-630` 是**预算漂移告警机**，而 `BIBLE.md` 的 P1/P8 明文引用了这条告警链。删除它属于 §3.3 的「能力变更」类，需要在 §9.2 的载体同步里一并处理 `BIBLE.md`（`BIBLE.md:343-347` 另有把 reviewer 覆盖上限归因于「upstream Claudexor capability」的表述，随工作面 C 失效）。

**其余连带面**（本规格点名，实施计划逐处核）：owner 唯一的预算设置入口 `settings_setup_contract._BUDGET_FIELDS`（`:124-162` → `settings.js` / `onboarding_wizard.js`）；web 侧除 `costs.js` 外还有 7 个模块读成本字段，含 `chat.js:204-209/643-648` 的 header 预算药丸与 `chat_activity.js:252-345` 的 `headerBudgetPresentation`/`taskCostMeta`/`taskCostProjection`；`devtools/benchmarks/swe_bench_pro/e1v2/run_pro.py:1016-1018` 的 campaign **以 `spent>=budget` 为唯一停止条件**。

### 6.7 循环内删除（终止出口 8 类 → 7 类）

| 位置 | 机制 |
|---|---|
| `loop.py:247 _check_budget_limits` | 三条预算轴（钱） |
| `loop.py:363 _resolve_task_cost_ceiling` | `task_pacing.resolve_cost_ceiling` |
| `loop.py:425 _soft_land_exhausted_ceiling` | 软着陆 |
| `loop.py:5734 _handle_budget_exceeded` | `BudgetExceeded` 异常路径 |
| `task_pacing.py` 的 `CostCeiling` 三态 | |

删除后的终止出口（「预算」整类消失）：

1. 轮数超限（`loop.py:6192`）
2. provider 不可用（`loop.py:6314`）
3. supervisor `finalize_now`（`loop.py:6218`）
4. 真实 deadline 本地收尾（`loop.py:2832`）
5. 模型自答（`loop.py:6334`）
6. transport 等待耗尽（`loop_transport.py:248-330`）
7. delegate hold（`loop.py:6207` / `:6308`）

另两条非循环归属、不受影响：派发前的 deadline 拒绝（`loop_llm_call.py:396`，不发车而非终止）与外部上限（supervisor 6h、owner Stop，`loop_transport.py:266-268` 明说不在此复制）。

### 6.8 必须保留

**`ouroboros/context_budget.py` 不要删。** 它是 **token / 字符**预算：`OWNER_LOW_TARGET_TOKENS = 200_000`、`MAX_RECENT_CHAT_TAIL = 1000`、`CONTEXT_OVERFLOW_CODES`。消费者：`llm.py:108/236`、`context_fit.py:305/382`、`main_context_authority.py:15`、`agent_startup_checks.py:711/794`、`request_wire_recovery.py:578`。

同理保留 `context_fit.measure_main_fit` 与 reclaim 三级节拍（是上下文回收，不是计费），以及 `loop_llm_call` 的 token 计数（`context_fit` 依赖它）。

**evolution 的节流功能必须保留（§6.3）**：`EVOLUTION_BUDGET_RESERVE` 的货币实现随计费删除，但「自迭代必须有自动停止条件」这条功能不能一起消失。替代实现是 campaign 级 cycle 上限（campaign 已按 `cycle: int` 计数，`evolution_lifecycle.py:443`），或把 `waiting_for_idle` 收紧为唯一节流。**保留下来的是失败暂停（`consecutive_failures >= 3`）、runtime mode 门、owner `/evolve on|off`、idle 节流——它们都不依赖计费。**

### 6.9 审计补充：§6.1 的二分要改成三分

审计逐个判定 `usage_accounting.__all__` 的 **40 个符号**，发现**二分法漏了第三类**——**归因 / 身份 / 跨层错误类型**，凡是按「名字像钱」扫的都会误删：

| 类 | 符号 | 依据 |
|---|---|---|
| 托管（保） | 见 §6.1 表 | — |
| 计费（删） | 见 §6.4 表 | — |
| **归因 / 身份**（**两边名单都没收，共 8 个**） | `AttemptRequest`（携带 model/provider/route_is_loopback/candidate_* 哈希，只有 2 个钱字段）、`AttemptReservation`（**托管转移的句柄**，无它则 `mark_dispatched`/`mark_unresolved`/`settle_attempt` 无法表达）、`UsageScope`/`usage_scope`/`current_usage_scope`（task/root/parent/category/source/**review_skill/wave_id/slot_id** 是身份，只有两个 `*_limit_usd` 是钱）、`capture_attempt_ids`（评审**执行收据**的凭据链，消费于 `review_execution_projection.py:58-63` 的 `_API_EXECUTION_RECEIPT_KEYS`）、`_usage_rows.REVIEW_ATTRIBUTION_KEYS`（委派 registration 的**幂等/指纹**字段，消费于 `delegate_registration_policy.py:23/77-78/100-101`） | 若按钱删，评审「按物理 attempt id 证明执行」与委派 registration 幂等同时断 |
| **跨层错误类型**（**未点名，共 3 个**） | `UsageAccountingError`、`UsageLedgerCorrupt`、`PhysicalAttemptPreparationFailed` | 20+ 个**保留**模块用它们区分「rail 失败 ≠ provider 故障」（`llm.py:2583-2584/3217-3241/4065-4172`、`supervisor/events.py:2524-2596` 等）；`PhysicalAttemptLimitExceeded` **继承**它们，托管迁移必须带上 |

**另有两处名字像计费、实为能力/凭据**（原判删除，**审计建议改判**）：

| 符号 | 真实职责 |
|---|---|
| `review_wave_admission` | 三个消费点里两个是**能力闸门**：`loop.py:1320-1345` 评审 wave 是否发车、`gateway/control.py:646-757` **managed-update 的采纳地板**（→ 409，`web/modules/updates.js:512-517` 渲染）。需像 §6.3 对 evolution 那样逐闸门判存活 |
| `skill_review_usage` | 是**评审覆盖度证据**：`skill_review_runner.py:210-247` 把物理行（含「dispatched/unresolved 且无 actor 记录」）并入 `executions`，驱动 `gateway/extensions.py:1150-1180` 的覆盖度披露。不是成本路由 |

**supervisor 的预算权威族**（§6.3 只点了 `EVOLUTION_BUDGET_RESERVE` 一个）：`supervisor/state.py:268-362` 的 `TOTAL_BUDGET_LIMIT`/`set_budget_limit`/`refresh_budget_from_settings`/`budget_remaining`/`reset_per_task_budget`（含 **P8 隔离守卫**）与 `:471 budget_pct`、`:487 update_budget_from_usage`、`:634 budget_breakdown`。其中 `budget_remaining:288-320` 的 docstring 明说「corrupt or unavailable monetary ledger **fails closed** … the supervisor cannot dispatch against stale counters」，是 **supervisor 侧 7 个派发口的共同准入**（`queue.py:1457-1460`、`workers.py:1254-1262`/`:1545-1552`/`:3852-3881`、`evolution_lifecycle.py:181-186`、`post_task_evolution.py:426-433`、`gateway/control.py:599-757`、`message_bus.py:141/1034-1063`）。**需逐个判存活。**

**`update_budget_from_usage` 在 7 个被明确保留的模块里被调用**（`agent_task_pipeline.py:1224/1279/1310`、`improvement_backlog.py:585`、`post_task_evolution.py:278`、`reflection.py:470/701`、`semantic_dedup.py:126`、`safety.py:1013`、`supervisor/events.py:654`）——§5.9/§5.12 保留了这些模块，却没说这条边怎么断。

### 6.10 owner 可见的钱面（BIBLE P8，需显式表态）

审计发现原稿只点名 `web/modules/costs.js`，实际「owner 能看到的钱」是一整片，且 **BIBLE P8 明文**要求它（`BIBLE.md:648-653`「Budget is a finite resource, and awareness of it is part of agency… Budget tracking integrity matters」）：

| 面 | 位置 |
|---|---|
| 唯一的预算设置入口 | `settings_setup_contract.py:124-162` 的 `_BUDGET_FIELDS`（`TOTAL_BUDGET`/`OUROBOROS_PER_TASK_COST_USD`，note「Keep this editable even after onboarding」）→ `web/modules/settings.js:597-600/767-770`、`onboarding_wizard.js:42/603/1295`（`STEP_META.budget` 步骤）、`costs.js:209-258` |
| **预算漂移告警整链**（P1「期望 vs 实际差异 → 立即告警」的唯一实现） | `supervisor/state.py:550-630`（`budget_drift_warning` 事件）、`context_health.py:267-274`（agent 上下文里的 `WARNING: BUDGET DRIFT`）、`context.py:1296-1300`（state 键投影）、`message_bus.py:1006-1030`（owner 消息尾部 budget 行） |
| owner 侧阈值推送 | `skills/telegram/lib/telegram_notifier.py:82-116` + `skills/telegram/plugin.py:1295-1298`（`TELEGRAM_NOTIFY_BUDGET` 80/90/100%）——**`skills/` 原稿未进消费者表** |
| web 头部药丸与任务成本 | `chat.js:93-117/204-209/643-648`、`chat_activity.js:252-345`（`headerBudgetPresentation`/`taskCostMeta`/`taskCostProjection`）、`utils.js:302-330`（`COST_ALIAS_PAIRS` 的 JS 镜像）、`api_types.js:9-12/286-305` |
| **跨语言 ABI** | `cost_projection.py` 的 docstring 自称 `cost_usd` 是「frozen wire contract, its removal would be a separate, explicitly approved ABI break」；`web/tests/wire_contract.test.js:52-66` 断言客户端读的 account 字段**必须**由服务端发射 |
| 模型可见的钱 | `context.py:385-420 _runtime_budget_info`（写进 cache-stable 前缀）、`task_pacing.py:445-521/560-680`（50/25/10% 阈值 + wrap-up note）、`nonprofitnanny_pacing.py:89-124`（`NANNY_REMINDER_USD` 钱轴）、`delegate_supervision.py:132-157`（`_settled_spend_fact`） |

**处置要求**：这些不是「代码减重」——`docs/CHECKLISTS.md:179` 的 item 21（capability_regression）要求**删除任何 user-facing 能力必须显式披露为能力变更**。因此本节整体从 §12.1（代码减重）移入 §12.2（能力变更），并把「预算自知与漂移告警」一并按 P8 走程序。**其替代形态（是否保留一个非货币的「消耗自知」面）由 Owner 定（§13）。**

## 7. 工作面 C — Claudexor：按**路由**裁，不按模块裁

### 7.1 它已有多条 native 旁路

| 面 | 路由表 | native 旁路 |
|---|---|---|
| 评审 | `review_execution.py:1383-1386` 的 `_REVIEW_ROUTE_EXECUTORS` 只有 `API_CHAT: ApiChatReviewExecutor` 与 `AGENT_SESSION: AgentSessionReviewExecutor`（后者即 Claudexor） | `:1407-1410` 对未实现路由抛 `ReviewRouteUnavailable`，即路由是**可选后端** |
| 子代理 | `subagents.py:334 resolve_subagent_executor` 的规则表内建 `auto` + harness 未配置 → **native child** 回退（D28）；`SubagentExecutorResolution` 的 executor 轴只有 native/harness 两值 | 同上 |

**因此 harness 路由是可选后端，不是唯一能力。** 按 26 个引用文件整片删会误伤；按 executor 轴逐路由裁才对。

### 7.2 逐路由裁除范围

| 对象 | 动作 |
|---|---|
| `AGENT_SESSION` 路由的 Claudexor 后端 | 删除，换 code agent 后端 |
| `API_CHAT` 路由（`ApiChatReviewExecutor`） | **保留不动** |
| `NativeToolRoundReviewExecutor` 分支（`_review_route_executor:1406`） | **保留不动** |
| `resolve_subagent_executor` 的 `auto → native` 回退 | **保留不动**，改造为 harness 后端缺失时的默认 |
| `claudexor_daemon.py`(978) / `claudexor_runtime.py`(1559) / `gateways/claudexor.py`(933) / `gateway/claudexor_accounts.py`(999) / `gateway/claudexor_quota.py`(41) / `claudexor_runtime_pin.json` | 删除（但 §5.7 要借用 `claudexor_runtime.py` 的 pin+校验形态给 Engram） |
| web 侧 | **§7.2 原稿写「无纯 harness 模块」是错的**——见下 |

**更正（审计证伪）**：`web/modules/claudexor_status_store.js:69-70` 整个模块**只有** `/api/claudexor/status` 与 `/wake` 两个常量，是**纯 harness 模块**，应整文件删除。原表另**漏列 4 个**：`harness_presentation.js`、`log_events.js`、`review_presentation.js`、`route_editor_primitives.js`。

| web 文件 | 总行数 | 命中 harness/账号的行 | 处置 |
|---|---|---|---|
| `web/modules/claudexor_status_store.js` | 954 | 纯模块 | **整删** |
| `web/modules/harness_accounts.js` | 1,363 | 234 | 逐分支删 |
| `web/modules/reviewer_slots.js` | 1,024 | 117 | 逐分支删 |
| `web/modules/harness_login_cards.js` | 1,588 | 88 | 逐分支删 |
| `web/modules/onboarding_agents_step.js` | 835 | 83 | 逐分支删 |
| `web/modules/subagents_settings.js` | 944 | 61 | 逐分支删 |
| `web/modules/subagent_status_primitives.js` | 109 | 44 | 逐分支删 |
| `web/modules/api_types.js` | 1,208 | 34 | 逐分支删 |
| `web/modules/settings.js` | 1,392 | 7 | 逐分支删 |
| `harness_presentation.js` / `log_events.js` / `review_presentation.js` / `route_editor_primitives.js` | — | 待逐处核 | **原表漏列** |

并同步 `web/tests/` 下对应的 13 个 `.test.js` 与 2 个 fixture（`credential_profiles_response.json`、`credential_profiles_response_unified.json`）。**python 测试面同样需枚举**（原稿零提及）。

### 7.3 `delegate_custody.py` 是接口契约，不是 Claudexor 私货

它的模块 docstring（`:1-7`）写明前置条件：被委派的 run 是「Ouroboros 不拥有进程树、能活过 worker 生命周期的可变异进程」，因此 custody **不能放模块字典**（崩溃/重启/丢 POST 都会留下无人能 wait/cancel/settle 的活 run）。

实现依赖三件事：

1. **自有 daemon 句柄** `ensure_owned_gateway`——§7.3 原稿只点了 3 处（`subagent_runtime.py:433/865`、`delegate_custody.py:1360`、`review_execution.py:653`），**审计实测 ≥14 处**，实施前需用 `lsp references` 取全集
2. **崩溃后仍可查的缺席原语**：`daemon_says_absent` + `close_absent_run`
3. **启动时和解**：`_reconcile_one`（`delegate_custody.py:1496`）

**换/加后端时按这三条验收。** 对照 §7.4 的 omp 能力面：

| custody 要求 | omp 侧对应 | 状态 |
|---|---|---|
| 持久 run 身份 | `--mode rpc` 会话 + `--provider-session-id` + `--session-dir` | ✅ 有 |
| 崩溃后可达 | `--resume <id>` / `--session <id>` | ⚠️ 有「恢复」，**需实测「查询存在性/缺席」的正向回答**（`daemon_says_absent` 语义） |
| 自有句柄 | 本仓自起 `omp --mode rpc` 子进程，用 `process_custody` 托管（沿用 `claudexor_daemon.py` 的 spawn 模板） | ✅ 机制已有 |
| 启动和解 | 需新建：rpc 的 `ready` 帧 + 会话列表查询 | 🔨 待建 |

**唯一可能真正的缺口**是「缺席查询」：`daemon_says_absent` 需要「**这个 run 不存在**」的正向回答，而 `--resume` 在会话不存在时的行为（错误码/退出码）尚未实测。这是本面唯一可能超出「删文件 + 改接口」范围的工作量。

### 7.4 语义落差（比我初判小）

Claudexor 是**长驻 daemon**：socket + `/v2` 控制 API + 并发会话 + 设备码登录。

**我初判 omp 只有一次性子进程（`omp -p`），这是错的。** 实测（omp `docs/cli-reference.md:154,163-203`、`docs/rpc.md:1-40`）：

| 能力 | omp 的对应面 |
|---|---|
| 常驻会话服务 | **`--mode rpc`** — newline-delimited JSON over stdio。启动先写 `ready` 帧，advertise `protocolVersion` 1/2 与传输上限（`maxFrameBytes` 1 MiB、`maxReassembledFrameBytes` 64 MiB）；协议 v2 有 `rpc_chunk` 无损分片；stdin 关闭时排空后退出码 0 |
| 标准 agent 协议 | `--mode acp`（Agent Client Protocol over stdio）、`acp` 子命令 |
| 运行身份持久化 | `--provider-session-id <id>`（原文「Reuse a specific provider-side session id **for continuity and cache scoping**」）、`--session-dir`、`--no-session` |
| 崩溃后可达 | `--resume [id]` / `--session [id]` / `--continue` / `--fork <session>` |
| prompt cache | `--prompt-cache-key <key>` |
| 单次调用 | `omp -p`（text 默认）/ `omp -p --mode json`（结构化事件流） |
| 有界运行 | `--max-time <duration>` |

**结论与决定**：§7.3 的 custody 契约要「**持久 run 身份**」，而**一次性 `-p` 无法满足**——每次调用都是新进程，没有可寻址的身份，崩溃后无从和解。因此本面的后端按 **`--mode rpc` 常驻会话**设计，不是 `-p` 单次委派：

| 需要 | 用什么 |
|---|---|
| 常驻会话 + 控制面 | `omp --mode rpc`（替代 Claudexor 的 socket + `/v2`）；`rpc.md` 有 `Session` / `Queue modes` / `Compaction` / `Retry` / `State` / `Prompting` 命令族，`/v2` 的控制面有对应落点 |
| run 身份 | `--provider-session-id <id>` + `--session-dir <dir>` |
| 崩溃后可达 | `--resume <id>` / `--session <id>`（配对 §7.3 的 `_reconcile_one`） |
| 单次运行上界 | `--max-time <duration>`（对应原 daemon 的 per-call `timeout_sec` 语义，`gateways/claudexor.py:301-311`） |
| 自有句柄 | 本仓自起该进程，用 `process_custody` 托管（沿用 `claudexor_daemon.py` 的 spawn 模板） |
| 协议版本协商 | `ready` 帧 advertise `protocolVersion` 1/2；v2 提供 `rpc_chunk` 无损分片 |

`--mode acp` 是备选（标准 Agent Client Protocol，若将来要接非本仓的宿主）。

仍成立的落差：账号由 code agent 自持（延续 `claudexor_daemon.py` docstring 的「Zero auth logic lives here」立场，是延续而非倒退）；钉定对象从「Node 运行时 + archive sha256」改为「code agent 版本 + 启动方式」。**唯一待实测的缺口**见 §7.3 的「缺席查询」。

### 7.5 删模块之外的连带断点（审计发现，规格原稿零提及）

被删的 6 个模块只是冰山。以下四条链挂在它们上，且**都不是「少个功能」而是「静默失去护栏/能力」**：

| # | 断点 | 证据 | 为什么危险 |
|---|---|---|---|
| 1 | **panic 路径静默吞 ImportError** | `server_control.py:161-165`：`from ouroboros.claudexor_daemon import get_owned_daemon` 在 `except Exception: pass` 内 | 删模块后 **panic 不再杀掉委托 run 的进程组**——紧急停止失效，且没有任何报错。**所有被删模块的 import 点都必须改为显式处理，尤其吞异常的路径** |
| 2 | **containment 验证护栏** | `delegate_containment.py:132/184` 是 `gateways.claudexor.attempt_containment` / `operator_home` 的唯一读者；`_widened_access` 与 `_home_isolation_breach` 是「access 被放宽」与「HOME 隔离未生效」的护栏 | 随网关消失后，子代理**越权访问与 HOME 隔离失效都不再被发现**（且它有一条持久披露行） |
| 3 | **派发前准入闸门 `route_health`** | `subagents.py:419-491`：`agent_capabilities` / `accessProfilesSupported` / 引擎版本 floor / `_exhausted_window`；消费点 `tools/plan_review_runtime.py:806-846`（**付费前健康快照**）与 `:965/1048-1052/1139-1143`（跳过行） | §7.2 只说保留 `resolve_subagent_executor` 的回退，**没说这一整套准入判定的归属**。它若不迁，派发会绕过健康检查 |
| 4 | **首装 onboarding 的订阅编译链** | `gateway/onboarding.py:441-446` 的 `_read_harness_snapshot` → `claudexor_accounts._status_payload` 是首装预设的唯一实读；`:460-469` 删模块后只剩 `PresetFailure(daemon_unavailable)` | **§12.1 把本面标为「能力不变」，但首装的「订阅→评审行/子代理行」编译链会断**——这是安装面能力，不是接口细节 |

**另有 11 项中危**，实施计划逐处核：`delegate_progress.py:358/367/468` 的超时常量与异常类型全 import 自被删网关；`subagent_work_order.py:111-124` 的 `route_source_request_channel` 能力证据来自 Claudexor manifest（消费者 `subagent_bootstrap.py:646`、`subagent_runtime.py:876`、`delegate_interactions.py:446`）；`review_thread_continuity.py` **整模块**是 v3 thread 操作，换后端后成孤儿（唯一消费者 `review_execution.py:737/811/895`）；`reviewer_window.py:38-45/110-126` 的 `SESSION_ROUTE_PROVIDER` 与「agent_session 行的 model_id 是 opaque harness 路由 spec」语义无落点；`config.py:141-142/331-359/1413-1416` 的 **4 个 `CLAUDEXOR_*` 常量 + 2 个 timeout getter + 2 个 `SETTINGS_DEFAULTS` 项**唯一生产消费者全在被删模块里。

**构建/发布面（原稿完全未提）**：`scripts/release_proof.py:55-60` 把 `embedded_claudexor_runtime` 列在 `COMMON_SMOKE_CHECKS` **必需集**；另有 `build.sh:66-67`、`build_linux.sh:65-66`、`build_windows.ps1:80-82`、`Ouroboros.spec:92-100`、`scripts/fetch_claudexor_runtime.py` 的 import，以及 `.github/workflows/{ci.yml, claudexor-platform-gate.yml, dependency-graph.yml}` 三个 workflow（含 3-OS platform gate）。**删 Claudexor 会同时红掉 release 必需检查与 CI。**

**棘轮账**：`size_ratchet_manifest.py:24/67` 点名 `tests/test_claudexor_owned_daemon.py` 与 `ouroboros/claudexor_runtime.py`，删文件必须**同 commit** 改账（§10 已有该纪律，此处补具体条目）。

**载体（§9.2 表遗漏）**：`docs/DELEGATED_ADMISSION.md:1-13` 自述「change it in the same commit as the code」，并具名 4 个 owner（`config.py` 两个 floor、`subagents.route_health`、`gateways.claudexor.attempt_containment`、`tools/delegate.py`）——**全部在本面被删改**；`BIBLE.md:343-347` 把 retrieving reviewer 覆盖上限归因于「upstream Claudexor capability」，随本面失效。

## 8. 工作面 D — 循环

**不改形态**。`run_llm_loop`（`loop.py:6087`，296 行）的轮顶顺序、tool 回填、终止判定保持原样。

**顺带做减法**（棘轮单向）：LoopScout 判为「纯行数/字节规避」的抽取物，在 A/B/C 触碰范围内合回——`_account_compaction_usage`（`loop.py:2631`，自陈「for the 300-line function gate」，真实逻辑 3 行）、`_force_plan_*`（`loop.py:196/216/222`，`owner_hurry.py:455` 自陈 byte-neutral extraction）、`context.py:_project_room_fact`(325) / `_OWNER_CLIENT_NOTE`(311)（自陈「to keep that builder under the hard method gate」）。

`loop.py` 的 `BYTE_DEBT = 284435`（不可变基线 `BYTE_BASELINE_DEBT = 335600`）**只能缩不能增**，所以这批是机会。

**保留**：`loop_transport.py`（provider 降级节拍）、`delivery_protocol.py`（协议解析器）、`acceptance_dialogue.py`（loop 刻意 re-export 以保单一 import 面）、`empty_round_guard.py`——都是真边界。

## 9. 执行顺序与同 commit 的同步要求

### 9.1 顺序

1. **B 删计费投影**（托管台账先迁出 `usage_accounting` → `physical_attempt.py`，再删计费，再按 §6.3 换 evolution 刹车）——耦合最低。
2. **C 按路由裁 harness**——对 `AGENT_SESSION` 路由换后端；API_CHAT 与 native 分支不动。
3. **A 接 Engram + 记忆改造**——最大；含 §5.7 二进制供给、§5.8 memory protocol、§5.14 迁移。

### 9.2 文档 / 提示词 / 版本载体必须同 commit 更新

这不是风格要求，是 P6 + P7 的硬约束：

- `BIBLE.md:604-607`（P7「Named canonical locations」）把 `docs/ARCHITECTURE.md`、`docs/DEVELOPMENT.md`、`docs/CHECKLISTS.md`、`docs/DESIGN.md` 列为具名 SSOT，并要求「**When a rule or fact moves, all references update in the same commit.** … A self-describing system that says two different things about itself cannot see its own contradiction.」
- `BIBLE.md:517`（P6）称 `docs/ARCHITECTURE.md` 为「Operational Map of the Body」——**身体的图说不能与身体不符**。
- P7 另有「Prompts are code — treat them with the same discipline」。

**本规格要删/改的子系统在这些载体里有大量描述，而规格此前从未点名它们：**

| 载体 | 行数 | 本规格的影响面 | 证据 |
|---|---|---|---|
| `docs/ARCHITECTURE.md` | 3,227 | **89 处 `claudexor`**；整节 `### Usage ledger substrate vs. accounting policy`(:1270)、`### Delegated subagents (Claudexor transport + the nanny)`(:1273)、`### Review delivery (retired Claude runtime)`(:1260)、`#### Background consciousness and Evolution`(:1152)、`## 1 … ### Data layout (~/Ouroboros/)`(:563) 会失实；`usage_accounting` 7 处、`cost-breakdown` 2 处 | `grep` |
| `docs/DEVELOPMENT.md` | 2,884 | 模块模式 / 文件大小预算若点名被删模块 | 待逐处核 |
| `docs/CHECKLISTS.md` | 859 | 评审清单里的记忆 / 计费 / 委派条目 | 待逐处核 |
| `docs/DESIGN.md` | 412 | UI 语义若含 Agents 页（§7.2 删 harness/账号面） | 待逐处核 |
| **`docs/DELEGATED_ADMISSION.md`** | — | **原表遗漏**。它自述「change it in the same commit as the code」，并具名 4 个 owner（`config.py` 两个 floor、`subagents.route_health`、`gateways.claudexor.attempt_containment`、`tools/delegate.py`）——**全部在 §7 被删改**（`:1-13`） | 审计 |
| **`BIBLE.md`** | 871 | **原表遗漏**。`:343-347` 把 retrieving reviewer 覆盖上限归因于「upstream Claudexor capability」；P1/P8 明文引用 `supervisor/state.py:550-630` 的预算漂移告警链（§6.6） | 审计 |
| `prompts/SYSTEM.md` | 910 | 记忆工具面（§5.6）、成本/预算表述、委派（§7） | 待逐处核 |
| `prompts/CONSCIOUSNESS.md` | 151 | BG 的工具白名单与协议——**§5.6.1 已收窄为允许接口适配** | 待逐处核 |
| `prompts/SAFETY.md` | 45 | 若点名被删路径 | 待逐处核 |
| **构建 / 发布面** | — | **原表遗漏**：`scripts/release_proof.py:55-60` 把 `embedded_claudexor_runtime` 列在 `COMMON_SMOKE_CHECKS` **必需集**；`build.sh:66-67`、`build_linux.sh:65-66`、`build_windows.ps1:80-82`、`Ouroboros.spec:92-100`、`scripts/fetch_claudexor_runtime.py`；`.github/workflows/{ci.yml, claudexor-platform-gate.yml, dependency-graph.yml}` | 审计 |
| **`ouroboros/size_ratchet_manifest.py`** | — | **原表遗漏**：`:24/67` 点名 `tests/test_claudexor_owned_daemon.py` 与 `ouroboros/claudexor_runtime.py`，删文件必须同 commit 改账 | 审计 |

**`ARCHITECTURE.md` 的版本行是一个被检查的载体**，不是纯文档：`context_health.py:253-256` 读它并比对版本（`desync_parts`）、`agent_startup_checks.py:403-411` 在启动时检查。所以「改了子系统却没改这个载体」会触发真实检查。

**要求**：工作面 A / B / C 各自落地时，**同 commit** 更新上表中受影响的载体；`ARCHITECTURE.md` 的版本载体随之移动（沿用该文件 §10「Key Invariants」第 2 条「Release metadata has one projection」的既有机制，由 `ouroboros/tools/release_sync.py` 承载）。上表末尾四项的逐处核查属于实施计划的第一步，不在本规格内断言具体改法。

## 10. 风险与对策

| 风险 | 证据 | 对策 |
|---|---|---|
| **违宪：外置了受保护的 durable memory** | `BIBLE.md:382-386`（Ship-of-Theseus）、`:604-607`（具名 canonical location） | §3.4 已划边界：`patterns.md` / `improvement-backlog.md` / `identity.md` 不外置，只镜像。若 Owner 要外置，走修宪 plan review |
| **违宪：记忆静默截断** | `BIBLE.md:113-114` | §5.2 独立通道（不能挂在默认关闭的 MCP 集成上）+ §5.11 降级契约（缺口显式标记，不渲染为空） |
| **违宪：历史被搬走** | `BIBLE.md:66` | §5.14 迁移是复制不是搬移；原文件一律保留 |
| **违宪：以 P7 名义删能力** | `BIBLE.md:571-575`「Minimalism is about code, not capabilities」 | §3.3 把取舍分「代码减重」与「能力变更」两类；§12.2 列出需程序的三项 |
| **违宪：把 identity 当记忆处理** | `BIBLE.md:40-41`「Not a config and not memory, but direction」 | §5.6/§5.14：identity 保留文件写入，不进 Engram |
| **误动 P0 命名的主动性实现** | `BIBLE.md:42-45` 明列 background consciousness 为 P0 实现；它是本仓两个 agent 职责之一 | §5.13：本规格不动它。减重是独立子项目，先做对照分析 |
| **删错层：把物理托管当账单删掉** | `loop_llm_call.py:799-802` 靠它禁止重发 | §6.1/§6.2 按投影层切，托管迁出保留 |
| **删错层：连 evolution 的成本刹车一起删掉** | `EVOLUTION_BUDGET_RESERVE`（`state.py:269`，美元）闸在 `evolution_lifecycle.py:189`、`queue.py:1506`、`workers.py:4010/4025`；而 §2 要保留自迭代 | §6.3/§6.8：保留**节流功能**、换非货币实现（cycle 上限）；失败暂停与模式门本就非货币，自动存活 |
| **删错层：把 BG 永久刹死** | `consciousness.py:675-691 _check_budget` 是 BG 唯一自动停止，读 `usage_projection`；删除后其 `except` 分支 fail-closed → BG 永久 `budget_blocked`，而 §5.13 声明不动它 | §6.5：删除计费时必须同时处理 `_check_budget`（改非货币轮次上限，或显式摘除并记账） |
| **删错层：`pricing.py` 里有安全闸门** | `infer_api_key_type` 是 `safety.py:600-623` 后端可达性闸门的输入，`:603-606` 且**静默 fail-open**；平铺删除 → 闸门永久放行 | §6.4：`pricing.py` 按符号切，`infer_*` 迁出保留 |
| **悬空引用：panic 路径静默吞 ImportError** | `server_control.py:161-165` 的 `from ouroboros.claudexor_daemon import get_owned_daemon` 在 `except Exception: pass` 内 → 删模块后 panic **不再杀掉委托 run 的进程组** | §7.2：所有被删模块的 import 点必须改为显式处理，**尤其吞异常的路径** |
| **违宪：身体的地图与被删子系统不符** | P7 `BIBLE.md:604-607`「all references update in the same commit」；P6 `:517`「Operational Map of the Body」；`docs/ARCHITECTURE.md` 含 **89 处 `claudexor`**、7 处 `usage_accounting`、整节 Usage-ledger 与 Delegated-subagents，而规格此前零提及 | §9.2：三个工作面各自**同 commit** 更新 `docs/ARCHITECTURE.md` / `DEVELOPMENT.md` / `CHECKLISTS.md` / `DESIGN.md` / `prompts/SYSTEM.md` / `CONSCIOUSNESS.md` / `SAFETY.md` |
| 版本载体 desync | `context_health.py:253-256` 读 `ARCHITECTURE.md` 版本并比对（`desync_parts`）；`agent_startup_checks.py:403-411` 启动检查 | 版本载体随文档同 commit 移动（`tools/release_sync.py`） |
| `size_ratchet` CI lane 阻塞 | `GIANT_PATHS` 含 `loop.py` `llm.py` `server.py` `supervisor/workers.py` `tools/control.py`，48 条精确记账 | 删除是棘轮允许方向；`BYTE_DEBT` 与文件同 commit 移动；用 `scripts/regenerate_size_ratchet.py` 重新生成 |
| 测试直接 import 私有符号 | `_drain_incoming_messages`（`test_available_subagents_core_followup.py:312`）、`_check_budget_limits`（`test_budget_limits.py:10`）、`maybe_inject_finalization_nudges`（`test_delegation_phase_b.py:301`）、`seal_task_transcript`（`test_anthropic_empty_block_fix.py:13`） | 下划线前缀在本仓是名义上的；删除前 `lsp references` 核对 |
| Engram 项目解析歧义 | 本仓一个 MCP server 下操作多项目目录 | §5.3 透传 typed 失败，禁止吞错误后降级写错项目 |
| Engram 可用性 | 外部二进制依赖 | §5.7 本仓供给 + pin 校验；缺失时 fail-fast，不静默降级成「无记忆」（会退化成每次冷启动） |
| tier-0 变空 | `context_layout.py` 的 `TIER0_ALWAYS_FULL` 是数据不变量 | §5.11 分级降级契约 |
| account 级知识读路径 | Engram 读默认按 project 分域 | §5.5 用 `scope: personal`/`global` 并传显式 scope，不依赖 cwd 解析 |

## 11. 验证策略

**A 面（记忆）**
- 现象：一次会话 `mem_save` 一条知识 → 重启运行时 → 新会话 `mem_search` 取回。
- **读路径（§5.5）**：写入一条 `scope: global` 的 account 级 observation → 在**任意一个 project** 的任务里确认它被召回；同时确认 project 级观察没有串味。tier-0 的 `identity` / `patterns` / `improvement-backlog` 走文件通道，不经此路径。
- **捕获期（§5.4）**：同一次任务中 max 与 low 两次投影的 `core_sha256` 一致。
- **降级（§5.11）**：停掉 Engram → tier-0 的 Engram 块显示显式缺口标记，且 `identity` / `patterns` / `improvement-backlog` **仍然渲染**。
- **迁移（§5.14）**：迁移后原文件仍在原位（逐文件断言）；`mem_search` 能命中迁移前 `knowledge/` 的已知 topic。
- **自迭代链（§5.9）**：一次触发反思的任务后，`improvement-backlog.md` 出现新候选，且 `maybe_promote` 的输入仍来自 `reflection_entry`。
- **反例**：把 Engram 条目的 `enabled` 改 false → **不影响** Tier-0（§5.2 的独立通道生效）。

**B 面（计费）**
- 一次正常任务跑完不抛 `BudgetExceeded`；cost-breakdown 路由 404。
- **托管回归**：构造一次 dispatched/unresolved 的发送，确认同模型重试仍被禁止（`loop_llm_call.py:799-802` 行为不变）。
- **evolution 刹车回归（§6.3）**：`remaining < EVOLUTION_BUDGET_RESERVE` 这个场景已随计费消失，改为逐条验证存活闸门——① 达到 cycle 上限 N 后 campaign 按选定语义停下；② `consecutive_failures >= 3` 仍能暂停；③ `light` 模式仍硬阻断（`evolution_block_reason`）；④ `/evolve off` 仍能停；⑤ `waiting_for_idle` 仍生效。
- 终止出口回归：逐条触发 7 类剩余出口。

**C 面（harness）**
- `AGENT_SESSION` 路由换后端后成功产出一份 review。
- `API_CHAT` 路由与 native 分支**不变**（回归）。
- harness 后端缺失时走 `auto → native child`（`subagents.py:334` 规则表），报账结构完整（`:399/:408/:472`）。
- **custody 三条件**（§7.3）：run 存活跨 worker 生命周期；崩溃后 `daemon_says_absent`/`close_absent_run` 仍能回答；`_reconcile_one` 启动时和解正确。

**D 面（循环）**
- 冒烟任务：提交 → 多轮 tool-use → 收尾，行为与重构前一致。
- `size_ratchet` lane 通过。

## 12. 取舍台账（按 §3.3 分两类，不可混同）

### 12.1 代码减重 — 能力不变，P7 背书，常规工作

| 项 | 内容 | 后果 |
|---|---|---|
| 情景/语义记忆外置 Engram | 只外置情景压缩、语义知识、反思正文、prompt 历史 | account 级知识用 `scope: personal`/`global` 跨 project 可见，project 级随 canonical project 解析（§5.5） |
| 上下文注入换源 | `build_memory_sections` / `build_knowledge_sections` / `build_recent_sections` 改读 Engram | 3 段 cache_control 结构与 `core_sha256` 契约不变 |
| 计费投影删除 | 删 `pricing`/`cost_projection`/`_usage_*`/`costs.js`/cost-breakdown 路由 | 终止出口 8 类 → 7 类 |
| 托管台账迁出而非删除 | `usage_accounting` → `physical_attempt.py` | 模块改名 + 迁移成本；保住「不重复发送」能力 |
| evolution 刹车换实现 | `EVOLUTION_BUDGET_RESERVE`（美元）→ campaign cycle 上限 | 保住「自迭代有自动停止条件」；失败暂停/模式门/idle 节流本就非货币，不受影响 |
| Claudexor 按路由裁 | 只裁 `AGENT_SESSION` 路由后端 | 后端改为 `omp --mode rpc` 常驻会话（§7.4）；API_CHAT 与 native 分支不动 |
| 循环内合回行数规避物 | `_account_compaction_usage`、`_force_plan_*`、`_project_room_fact` | `loop.py` 字节棘轮只能缩，这是机会 |
| 不采用 pi | 循环留 Python | 形态不变；12 项治理不需跨语言重建 |

### 12.2 触碰 P0/P1/P3 的四项 — 前三项「不动」，第四项需 Owner 定

下表不是「取舍」，而是记录**为什么这些项在本规格里没有可选的余地**（第四项除外，它需要 Owner 定替代形态）：

| 项 | 为什么不能在本规格里改 | 本规格的处理 |
|---|---|---|
| **`consciousness`（删或合并）** | `BIBLE.md:42-45` 把它列为 P0 的具名实现；它还与本仓已定的「两个 agent 职责」分界重合（独立 registry + 白名单强制）；`:25-27` P0–P4 不可互废；`:10-11` 宪法变更须经 explicit reviewed release | **完全不动**——不删、不合并、不在 §5.12 的删除清单内。减重是**独立子项目**，需先做「哪些是真重复、哪些是角色必需」的对照分析（§5.13） |
| **把 `patterns.md` / `improvement-backlog.md` 外置** | `BIBLE.md:382-386` 明确二者「never abandoned or replaced wholesale」且共享宪法核心的 Ship-of-Theseus 保护；`:126-129` 明说 durable-memory permanence 的改动「is itself a constitutional change and requires plan review」 | **不外置**，只建只读索引（§3.4） |
| **把 `identity.md` 做成记忆条目** | `BIBLE.md:40-41` identity「Not a config and not memory, but direction」；`:37-38` 文件须持续存在 | **不动**，既不外置也不做 topic |
| **删除 owner 可见的钱面**（§6.10） | `BIBLE.md:73-76` P1 明文的「身体状态自省 + 期望/实际差异立即告警」；`:648-653` P8「awareness of it is part of agency … tracking integrity matters」；`docs/CHECKLISTS.md:179` item 21 要求 user-facing 能力的删除必须显式披露为能力变更 | **整体从 §12.1 移入本表**。这不是代码减重——预算是 BIBLE 明文的 agency 面。**其替代形态（是否保留一个非货币的「消耗自知」面）由 Owner 定（§13）** |

**这四项里前三项是「不动」，第四项（owner 可见的钱面）需要 Owner 定替代形态**。除此之外本规格全部属于代码减重。若要改变前三项中的任何一项，都是**另开一份规格**的事，且前两项须先走 P9 的发布流程。

## 13. 待定项

**已由本轮调研解决**（原待定项）：

- ~~Engram MCP 工具档~~ → 用 `--tools=agent`（19 个 agent 面向工具；不带该档为全部 23 个）。
- ~~`self/*` 的固定 project 名与 `.engram/config.json` 相容性~~ → 不需要固定 project。改用 Engram 的 `scope: personal|global`（§5.5）。
- ~~code agent 钉定形态~~ → omp 有 `--mode rpc`（常驻 JSON-over-stdio，带 `ready` 帧与协议版本协商）+ `--provider-session-id` + `--resume`，钉定对象是 omp 版本与启动方式（§7.4）。
- ~~memory protocol 的落点~~ → 从 Engram 自带的 `DOCS.md §Memory Protocol` 裁剪进 `prompts/SYSTEM.md`（§5.8）。

**审计新增的未决项（§14.3）**：

- **A 面落地前必须定**：`auto_resume_after_restart` 的判据（`workers.py:1671-1695` 现读 `memory/scratchpad.md`）。§2 声明不动 supervisor 生命周期，P1（`BIBLE.md:73-76`）又点名 scratchpad 为会话起点的核验对象。改为读 Engram，还是保留一个**专供该判据**的本地摘要文件？
- **§5.6.1 的三选一**：记忆工具保留旧名 / 改 BG 读证钩子（默认）/ 交给角色规格。
- **§6.5 的替代形态**：BG 的 `_check_budget` 改为非货币轮次上限，还是显式摘除并记账。
- **§6.10 的替代形态**：owner 可见的钱面是否保留一个非货币的「消耗自知」面（P8 要求 awareness 属 agency，但未规定必须是美元）。
- **§14.3-5**：C 面的首装 onboarding 编译链按 §3.3 属能力变更，需重新归类（§12.1 原标为「能力不变」）。

**仍未决**：

- §5.14 中 `task_reflections.jsonl` 的导入量（全量 vs 最近 N 条；**原文一律保留**已是定论）。
- §7.3 的缺口实测：`omp --resume <不存在的 id>` 的退出码/错误码，能否支撑 `daemon_says_absent` 的正向「不存在」回答。**这是本规格唯一可能超出「删文件 + 改接口」范围的风险点。**
- **§6.3 的 cycle 上限 N 取值**：这是「自迭代能跑多久」的语义变化，需 Owner 定。现状无任何 cycle 上限，替代 `EVOLUTION_BUDGET_RESERVE` 后 N 是唯一的定量上界。另需定：N 用尽后 campaign 是**暂停**（可恢复）还是**完成**（需重新 `/evolve`）。
- Engram 的 `scope: global` 是否会被 `all_projects=true` 之外的操作意外包含，需确认它对 tier-0 的语义（是否真的跨 project 可见且不污染 project 级召回）。
- Engram `--tools=agent` 的 19 项里，`mem_review` / `mem_judge` / `mem_compare` 是否暴露给主循环，还是只给评审面。属工具面配置，不阻塞规格。

**明确排除在本规格之外**：

- `consciousness` 的任何改动（§5.13）。它的减重是独立子项目，需先做对照分析。
- 交互层（presence 合并、hurry 降级、client_surface 删除）。

## 14. 遗漏审计（三面，只读，file:line 取证）

**方法**：三个独立审计（记忆面 / 计费面 / harness 面），各自读本规格对应章节后进代码核对**消费者、配置键、测试、文档**。原始产出带完整逐项表（会话内 `agent://AuditMemoryFace` / `AuditBillingFace` / `AuditHarnessFace`）。本节是整合与处置。

合计 **60 项**：高危 **16** / 中危 **34** / 低危 **16**。

### 14.1 核心结论：三个工作面都不是「模块删除」，而是「职责迁移」

已被证伪**六次**的「按模块删」假设——每次都有一个模块名 ≠ 实际职责：

| 模块名 | 真实职责比名字多出来的部分 |
|---|---|
| `usage_accounting` | 物理发送托管 + **归因/身份/跨层错误类型**（40 个 `__all__` 符号里 **15 个**两边名单都没收） |
| `memory.py` | `chat_history` 工具（`:453`）、`logs/*.jsonl` 尾部读取与渲染（`read_jsonl_tail`/`summarize_chat`）——**都是 §5.6/§5.10 声明保留的能力** |
| `consolidator.py` | task narrative 的 Light 车道路由、owner 消息源解析的世代链、**`index-full.md` 的唯一常规写者（tier-0 成员）** |
| `semantic_dedup.py` | **P2/P3 保护的两条免疫队列**（backlog 条目、review obligation）的去重器 |
| `pricing.py` | `safety.py` 的后端可达性闸门 `infer_api_key_type` |
| `claudexor*` 家族 | panic 的进程组 kill、containment 护栏、`route_health` 准入闸门、首装 onboarding 编译链 |

**由此得出实施纪律**：实施计划的第一道工序**不是「删除清单」，而是「每个被删模块的消费者清单 + 每个消费者的存活判定」**。§6.3 那种「逐闸门判存活」的表应成为三个工作面的统一模板。净删行数不再是有效指标；有效指标是**「多少条能力需要迁移落点」**。

### 14.2 高危 16 项的处置（均已在正文修正）

| # | 面 | 项 | 证据 | 处置 |
|---|---|---|---|---|
| 1 | A | **BG 身份闸门的两条输入源**（按工具名 `knowledge_read` 认账 + `dialogue-gap` 缺口） | `consciousness.py:1251-1265/1352-1367/1078-1081/1042-1048` | **§5.6.1**（新增）+ §5.13 收窄 |
| 2 | A | `Memory` 兼任日志/转写读取器 | `memory.py:453/544/558/850-885` → `context.py:1095/1100/1145/1152/1156` | §5.12 重分类 |
| 3 | A | supervisor 重启续跑判据是 `scratchpad.md` | `workers.py:1671-1695` | **未解决**，§14.3-1 |
| 4 | A | `semantic_dedup` 是免疫队列去重器 | `semantic_dedup.py:1-16`、`improvement_backlog.py:245`、`review_state.py:1414` | §5.12 **改判保留** |
| 5 | A | `index-full.md` 生产者链断（**tier-0 冻结**） | 写 `consolidator.py:646-673` 等 4 处；tier-0 `context_layout.py:51-59` | §5.11 |
| 6 | A | task narrative 借 consolidator 车道且失败被静默吞 | `agent_task_pipeline.py:1157-1158/1198/1217-1218/1235` | §5.12 |
| 7 | B | `consciousness._check_budget` 是 BG 唯一自动停止（删后 fail-closed 永久停摆） | `consciousness.py:675-691/640/831/696-713/690-692` | **§6.5**（新增） |
| 8 | B | `reserve_attempt`/`settle_attempt`/`AttemptRequest`/`AttemptReservation` 是托管状态机本体 | `usage_accounting.py:255/1020/684-772/211-252`；`loop_llm_call.py:799-806`、`review_custody.py:50-52`、`tools/search.py:590/673/689` | §6.4「第三类」 |
| 9 | B | `pricing.py` 里的 safety 闸门 | `safety.py:600-623`、`:603-606` fail-open | §6.4 |
| 10 | B | owner 唯一的预算设置入口 `_BUDGET_FIELDS` | `settings_setup_contract.py:124-162` → 4 个 web 模块 + onboarding 步 + 3 个测试 | §6.10 |
| 11 | B | **BIBLE P1/P8 命名的预算漂移告警整链** | `supervisor/state.py:550-630`、`context_health.py:267-274`、`message_bus.py:1006-1030`、`telegram_notifier.py:82-116` | **§3.2 新增 P1/P8 两行 + §6.10 + §12.2 移入能力变更** |
| 12 | C | panic 路径静默吞 ImportError（不再 kill 进程组） | `server_control.py:161-165` | §7.5 + §10 |
| 13 | C | containment 验证护栏（access 放宽 / HOME 未生效） | `delegate_containment.py:132/184`、`tools/delegate.py:138-144` | §7.5 |
| 14 | C | `route_health` 派发前准入闸门无归属 | `subagents.py:419-491`；消费 `plan_review_runtime.py:826-829` 等 5 处 | §7.5 |
| 15 | C | 首装 onboarding 的订阅编译链断 | `gateway/onboarding.py:441-446/460-469`、`subscription_install_presets.py` | §7.5 + §14.3-5 |
| 16 | C | `config.py` 的 4 个 `CLAUDEXOR_*` + 2 getter + 2 defaults 悬空 | `config.py:141-142/331/338/357/359/1413-1416` | §7.5 |

### 14.3 未解决（需 Owner 定或另立规格）

1. **A：`auto_resume_after_restart` 的判据**（`workers.py:1671-1695` 读 `memory/scratchpad.md`）。§2 声明不动 supervisor 生命周期，而 A 面必然改变该文件；**P1（`BIBLE.md:73-76`）又点名 scratchpad 为会话起点的核验对象**。必须在 A 面落地前定：改为读 Engram，还是保留一个**专供该判据**的本地摘要文件。
2. **§5.6.1 的三选一**（记忆工具保留旧名 / 改 BG 读证钩子 / 交给角色规格）。
3. **BG 的 `_check_budget` 替代形态**（§6.5）：非货币轮次上限 vs 显式摘除。
4. **§6.10 的 owner 可见钱面替代形态**：是否保留一个非货币的「消耗自知」面（P8 要求 awareness 是 agency 的一部分，但没说必须是美元）。
5. **C：首装 onboarding 编译链**——§12.1 原把 C 面整体标为「能力不变」，按 §3.3 这属能力变更，需重新归类。

### 14.4 中危 34 项（按类，实施计划逐处核）

**A 面（记忆，12 项）**：`project_dialogue.py:210-222` 的 owner 消息源解析 → 准入闸门 `authority_source_unavailable`（`agent_startup_checks.py:193-216`，调用点 `agent.py:775/1117` 无 try/except）；`presence_context.py:9/25-30` 用 `_sanitize_topic`；`registry.md` 的残余读者群（`context.py:997/1186-1190/1440`、`deep_self_review.py:49`、`headless.py:1143`）；记忆健康面 `context_health.py:289-312` + `agent_startup_checks.py:838-859`（scratchpad 两项随外置失效）；`evolution_checkpoints.py:185-196` 的三个 sha 常数化；`utils.py:1217-1226/1331-1343/1391-1396` 的记忆体积指标 → `web/modules/evolution.js:84-95` 图表；`gateway/control.py:120-141 api_reset` 的清空范围不含 Engram DB；**`memory_mode`（forked/empty/shared）的隔离语义**（`gateway/tasks.py:439-465`、`headless.py:127-165/1136-1165`）——Engram 按 project 分域后子 drive 不再是记忆边界；`deep_self_review.py:46-54/106-133` 的记忆白名单；`WORLD.md` 的生成者（`memory.py:438-448`）随删除失去重生成路径；**提示词/策略/配额表的死条目**（§5.6 第 2 类已列）；**内容钉定型测试**（`test_docs_sync.py:57-60`、`test_context_budget_ssot.py:79-81`、`test_scratchpad_consolidation.py:64-88`）；`reflection.py:31-32 NONTRIVIAL_COST_THRESHOLD` 的 USD 触发器随 B 面失效（跨工作面）。

**B 面（计费，14 项）**：`supervisor/state.py` 整套预算权威（`budget_remaining:288-320` 是 7 个派发口的共同准入、`reset_per_task_budget` 含 P8 守卫）；`review_wave_admission` 的三个消费点（其中 managed-update 采纳地板 `gateway/control.py:646-757` 原稿全文未提）；`capture_attempt_ids`/`ledger_attempt_ids` 的评审收据链；`skill_review_usage` 的覆盖度证据；**prompt/上下文里的钱数据面**（`context.py:385-420`、`task_pacing.py:369-370/445-521/560-680/745-760`、`delegate_supervision.py:132-157`、`agent.py:189-202`）；web 成本面远不止 `costs.js`（7 个模块 + `web/tests/wire_contract.test.js:52-66` 的跨语言 ABI 断言）；**`nanny` 的 USD 轴**（`task_pacing.py:48-63 NANNY_REMINDER_USD`、`nanny_pacing.py:89-124`）删后双轴退化单轴（4 个测试失效）；**`devtools/benchmarks` 以计费为唯一 campaign 停止**（`swe_bench_pro/e1v2/run_pro.py:1016-1018`）；`_usage_rows.REVIEW_ATTRIBUTION_KEYS` 被委派 registration 幂等复用；`UsageAccountingError`/`UsageLedgerCorrupt` 的跨层控制流（20+ 保留模块）；`update_budget_from_usage` 的 7 个保留模块调用点；`ensure_legacy_imported`/`IMPORT_REL` 的共享前置语义；`usage_from_response` 是 token 抽取落点却被整删 `_usage_response.py`；`record_unmetered_external_dispatch` 的 4 个 skill/extension 落点。

**C 面（harness，8 项）**：`delegate_progress.py:358/367/468` 的超时/异常契约来自被删网关；`subagent_work_order.py:111-124` 的 source channel 失据；`review_thread_continuity.py` 整模块孤儿（唯一消费者 `review_execution.py:737/811/895`）；plan review 的**付费前健康快照**（`plan_review_runtime.py:806-846/965/1048-1052`）；**`agent_session` 行的窗口/权威语义无落点**（`reviewer_window.py:38-45/110-126` + `scope_review_session.py:228-231` + `scope_review.py:1304-1327` + `gateway/settings.py:617-621`，散在 5 个模块）；**构建/打包/CI 面**（`release_proof.py:55-60` 的必需检查、`fetch_claudexor_runtime.py`、3 个构建脚本、`Ouroboros.spec`、3 个 workflow——`claudexor-platform-gate.yml` 被测对象消失却继续绿灯，**发布面拿到虚假保证**）；**python 测试面零枚举**（6 个整文件死亡 + `test_gateway_parity.py:14-20/225-298` 冻结契约需重写）；**交互面 `pending_interactions`/`delegate_answer` 无归属**（`gateways/claudexor.py:789-800` 是那条 wire 形状的唯一读者；omp rpc 无对应语义）；`delegate_custody.py:120-121/881-893` 的**引擎 wire 解析层**（含 7 天上界、applied-profile/access 取证）——custody 三条件不含这层，等于把「改接口」低估成 0；`size_ratchet_manifest.py:24/67` 点名两个待删文件。

### 14.5 低危 16 项

A：scratchpad 三个预算常量悬空（`context_budget.py:208/210/212`）；`config.py:227-228` 注释承诺的「low = 更深记忆整合」无实现；**第三类记忆文件无归属**（`deep_review.md`、`dialogue_summary.md`、`knowledge_journal.jsonl`、`patterns_history.jsonl`）；`memory/` 仍是协议存储的家（`owner_mailbox.py:13`）；`utils.py:152-156` 的注释语义失真。B：`physical_attempt_limit`/`_claim_physical_dispatch` 未进名单；`usage_from_response`（已列）；`record_unmetered_external_dispatch`（已列）；`ARCHITECTURE.md:105-112/1271-1273/2729-2740/2871-2875/2956-2957` 与 `DEVELOPMENT.md:1974-1994` 的 money 不变量章节。C：12 处注释/文档字符串残留；fixture 实际 **4** 个（原稿写 2）；`README.md:75/197` + `site/*` 的公开承诺**被测试钉死**（`test_public_site_metadata.py:145-157` 逐面断言 `claudexor.ai` 存在）——§9.2 载体表不含 README/站点；`CHECKLISTS_ARCHIVE.md:17`；`BIBLE.md:343-347`。
