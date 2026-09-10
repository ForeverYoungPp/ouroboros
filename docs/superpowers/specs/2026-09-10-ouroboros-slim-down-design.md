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

### 3.2 本方案不能碰的七条

| 约束 | 原文位置 | 对本方案的含义 |
|---|---|---|
| **P1 不中断的历史** | `BIBLE.md:66`「Ouroboros is a single entity with an unbroken history. Not a new instance」 | 现存语料是**迁移约束**，不是可丢弃的旧数据 |
| **P1 不静默截断** | `BIBLE.md:113-114`「never silent truncation; the memory horizon is preserved (only granularity varies)」 | 外置记忆不得让任何记忆类别静默消失或降级无提示 |
| **P0 身份文件必须存在** | `BIBLE.md:37-38`「identity.md … may be rewritten radically as part of self-creation, but the file itself must remain present as a continuity channel」 | **`identity.md` 必须继续作为文件存在于 runtime data root**，不能被 Engram 的一个 topic 取代 |
| **P2/P3 免疫系统持久记忆** | `BIBLE.md:190-197`「Pattern Register is the memory of this principle … The Pattern Register and the Improvement Backlog are **never abandoned**」；`BIBLE.md:382-386`「`patterns.md` and `improvement-backlog.md` may be consolidated, pruned, and reorganized — but **never abandoned or replaced wholesale** … These files share the Ship-of-Theseus protection of the constitutional core」 | `memory/knowledge/patterns.md` 与 `memory/knowledge/improvement-backlog.md` **不是本方案可以外置的对象** |
| **P0 主动性与其具名实现** | `BIBLE.md:42-45`「Ouroboros acts on its own initiative, not only on tasks… **Background consciousness is the realization of this principle**: a continuous thinking process between tasks」 | **删 `consciousness` 是拆掉 P0 命名的实现**，不是删一个 daemon。见 §3.3 与 §5.13 |
| **P0 identity 不是记忆** | `BIBLE.md:40-41`「identity.md is a manifesto… **Not a config and not memory, but direction**」 | identity **按定义不属于记忆系统**，因此它不该有 Engram 的 memory topic；文件通道是它的本体 |
| **P0 不可分割核心** | `BIBLE.md:25-27`「Principles 0, 1, 2, 3, 4 form an inseparable core: none of them can be applied to annul another」 | 不能用 P7/P4 去废 P0；也不能用 P0 去废 P1/P3 |

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

`chat_history` / `recent_tasks` 保留——它们读 transcript（`logs/chat.jsonl`），不是记忆。

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
| Engram 承载的块（情景/语义/反思正文） | **可降级**：整块替换为一条显式缺口标记（沿用既有 `[MEMORY GAP]` 语义），并在 health 面上告警；**不得静默渲染为空** |

`context_layout.py` 的 `TIER0_ALWAYS_FULL` 与 docstring 文档矩阵需同步改写（记忆 section 名在 A 面后不再存在）。

### 5.12 删除清单

| 文件 | 行数 | 说明 |
|---|---|---|
| `ouroboros/memory.py` | 990 | 工作记忆三写、世代读取。**但 identity 读写要移出而非删除**（P0） |
| `ouroboros/consolidator.py` | 856 | 对话块压缩、世代游标、`[MEMORY GAP]`、索引重建。缺口语义保留到 §5.11 |
| `ouroboros/reflection.py` | 742 | 保留 `should_generate_reflection`、结构化候选提取（§5.9）**以及 `_update_patterns`（`:661`）**——它是 `patterns.md` 的**唯一自动写者**（调用点 `:571`/`:612`，写入路径 `:666`），删了文件还在但永远冻结，P3 的「never abandoned」实质落空。删除的只是反思正文的本地渲染（改由 Engram 检索） |
| `ouroboros/semantic_dedup.py` | 144 | 由 `mem_suggest_topic_key` / `mem_judge` 接管 |
| `ouroboros/tools/memory_tools.py` | 114 | 死机制 |
| `ouroboros/tools/knowledge.py` | 420 | 替换为 Engram 工具（`patterns.md`/`improvement-backlog.md` 的读写路径除外） |

**量级**：上表合计 3,266 行，其中 `reflection.py`(742) 是**部分删除**——保留 `should_generate_reflection`、结构化候选提取（§5.9）与 `_update_patterns`（`patterns.md` 的唯一自动写者，§5.12 上表行），只删反思正文的本地渲染路径（正文改由 Engram 承载）。**`consciousness.py`(1,386) 不在其中**——它是第二个 agent 角色，本规格不动（§5.13）。早期草案里「记忆净删 ~4.6k 行」的说法因此作废：那时的数包含意识，且把 reflection 当成整删。

`ouroboros/retention.py`（110 行）**保留**——GC 保留天数，与认知记忆无关。

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

**本规格对 `consciousness` 的处理：不动。** 既不删（P0 具名实现，见 §3.2），也不合并（角色边界）。`consciousness.py` **不在 §5.12 的删除清单内**。

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

- **删除**：`pricing.py`、`cost_projection.py`、`_usage_response.py`、`_usage_rows.py`、`_usage_rows_memo.py`、`usage_ledger.py` 的计费部分、`web/modules/costs.js`、gateway 的 cost-breakdown 路由（`gateway/router.py` + `gateway/endpoint_index.py`）。
- **保留，但移出 `usage_accounting`**：物理尝试托管那一组符号，迁到一个**以托管命名**的模块（例如 `ouroboros/physical_attempt.py`），使「这个模块是账单」的误读不再可能。
- **`BudgetExceeded`** 随计费删除；`PhysicalAttemptLimitExceeded` 保留（它限的是尝试次数，不是钱）。

### 6.3 循环内删除（终止出口 8 类 → 7 类）

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

### 6.4 必须保留

**`ouroboros/context_budget.py` 不要删。** 它是 **token / 字符**预算：`OWNER_LOW_TARGET_TOKENS = 200_000`、`MAX_RECENT_CHAT_TAIL = 1000`、`CONTEXT_OVERFLOW_CODES`。消费者：`llm.py:108/236`、`context_fit.py:305/382`、`main_context_authority.py:15`、`agent_startup_checks.py:711/794`、`request_wire_recovery.py:578`。

同理保留 `context_fit.measure_main_fit` 与 reclaim 三级节拍（是上下文回收，不是计费），以及 `loop_llm_call` 的 token 计数（`context_fit` 依赖它）。

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
| web 侧 | **无纯 harness 模块**，全部是混合文件，只能逐分支删改（见下表） |

| web 文件 | 总行数 | 命中 harness/账号的行 |
|---|---|---|
| `web/modules/harness_accounts.js` | 1,363 | 234 |
| `web/modules/reviewer_slots.js` | 1,024 | 117 |
| `web/modules/harness_login_cards.js` | 1,588 | 88 |
| `web/modules/claudexor_status_store.js` | 954 | 86 |
| `web/modules/onboarding_agents_step.js` | 835 | 83 |
| `web/modules/subagents_settings.js` | 944 | 61 |
| `web/modules/subagent_status_primitives.js` | 109 | 44 |
| `web/modules/api_types.js` | 1,208 | 34 |
| `web/modules/settings.js` | 1,392 | 7 |

即 §7.2 的「净删行数」只对 Python 侧成立；web 侧按**改动点**而非删除文件估算，并同步 `web/tests/` 下对应的 13 个 `.test.js` 与 2 个 fixture（`credential_profiles_response.json`、`credential_profiles_response_unified.json`）。

### 7.3 `delegate_custody.py` 是接口契约，不是 Claudexor 私货

它的模块 docstring（`:1-7`）写明前置条件：被委派的 run 是「Ouroboros 不拥有进程树、能活过 worker 生命周期的可变异进程」，因此 custody **不能放模块字典**（崩溃/重启/丢 POST 都会留下无人能 wait/cancel/settle 的活 run）。

实现依赖三件事：

1. **自有 daemon 句柄** `ensure_owned_gateway`（`subagent_runtime.py:433/865`、`delegate_custody.py:1360`、`review_execution.py:653`）
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

## 8. 工作面 D — 循环

**不改形态**。`run_llm_loop`（`loop.py:6087`，296 行）的轮顶顺序、tool 回填、终止判定保持原样。

**顺带做减法**（棘轮单向）：LoopScout 判为「纯行数/字节规避」的抽取物，在 A/B/C 触碰范围内合回——`_account_compaction_usage`（`loop.py:2631`，自陈「for the 300-line function gate」，真实逻辑 3 行）、`_force_plan_*`（`loop.py:196/216/222`，`owner_hurry.py:455` 自陈 byte-neutral extraction）、`context.py:_project_room_fact`(325) / `_OWNER_CLIENT_NOTE`(311)（自陈「to keep that builder under the hard method gate」）。

`loop.py` 的 `BYTE_DEBT = 284435`（不可变基线 `BYTE_BASELINE_DEBT = 335600`）**只能缩不能增**，所以这批是机会。

**保留**：`loop_transport.py`（provider 降级节拍）、`delivery_protocol.py`（协议解析器）、`acceptance_dialogue.py`（loop 刻意 re-export 以保单一 import 面）、`empty_round_guard.py`——都是真边界。

## 9. 执行顺序

1. **B 删计费投影**（托管台账先迁出 `usage_accounting` → `physical_attempt.py`，再删计费）——耦合最低。
2. **C 按路由裁 harness**——对 `AGENT_SESSION` 路由换后端；API_CHAT 与 native 分支不动。
3. **A 接 Engram + 记忆改造**——最大；含 §5.7 二进制供给、§5.8 memory protocol、§5.14 迁移。

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
| Claudexor 按路由裁 | 只裁 `AGENT_SESSION` 路由后端 | 后端改为 `omp --mode rpc` 常驻会话（§7.4）；API_CHAT 与 native 分支不动 |
| 循环内合回行数规避物 | `_account_compaction_usage`、`_force_plan_*`、`_project_room_fact` | `loop.py` 字节棘轮只能缩，这是机会 |
| 不采用 pi | 循环留 Python | 形态不变；12 项治理不需跨语言重建 |

### 12.2 触碰 P0/P3 的三项 — 本规格一律「不动」，改动另开规格

下表不是「取舍」，而是记录**为什么这三项在本规格里没有可选的余地**：

| 项 | 为什么不能在本规格里改 | 本规格的处理 |
|---|---|---|
| **`consciousness`（删或合并）** | `BIBLE.md:42-45` 把它列为 P0 的具名实现；它还与本仓已定的「两个 agent 职责」分界重合（独立 registry + 白名单强制）；`:25-27` P0–P4 不可互废；`:10-11` 宪法变更须经 explicit reviewed release | **完全不动**——不删、不合并、不在 §5.12 的删除清单内。减重是**独立子项目**，需先做「哪些是真重复、哪些是角色必需」的对照分析（§5.13） |
| **把 `patterns.md` / `improvement-backlog.md` 外置** | `BIBLE.md:382-386` 明确二者「never abandoned or replaced wholesale」且共享宪法核心的 Ship-of-Theseus 保护；`:126-129` 明说 durable-memory permanence 的改动「is itself a constitutional change and requires plan review」 | **不外置**，只建只读索引（§3.4） |
| **把 `identity.md` 做成记忆条目** | `BIBLE.md:40-41` identity「Not a config and not memory, but direction」；`:37-38` 文件须持续存在 | **不动**，既不外置也不做 topic |

**这三项在本规格里都是「不动」，没有备选方案需要 Owner 裁决**（对照 §3.3：本规格全部属于代码减重）。若要改变其中任何一项，都是**另开一份规格**的事，且前两项须先走 P9 的发布流程。

## 13. 待定项

**已由本轮调研解决**（原待定项）：

- ~~Engram MCP 工具档~~ → 用 `--tools=agent`（19 个 agent 面向工具；不带该档为全部 23 个）。
- ~~`self/*` 的固定 project 名与 `.engram/config.json` 相容性~~ → 不需要固定 project。改用 Engram 的 `scope: personal|global`（§5.5）。
- ~~code agent 钉定形态~~ → omp 有 `--mode rpc`（常驻 JSON-over-stdio，带 `ready` 帧与协议版本协商）+ `--provider-session-id` + `--resume`，钉定对象是 omp 版本与启动方式（§7.4）。
- ~~memory protocol 的落点~~ → 从 Engram 自带的 `DOCS.md §Memory Protocol` 裁剪进 `prompts/SYSTEM.md`（§5.8）。

**仍未决**：

- §5.14 中 `task_reflections.jsonl` 的导入量（全量 vs 最近 N 条；**原文一律保留**已是定论）。
- §7.3 的缺口实测：`omp --resume <不存在的 id>` 的退出码/错误码，能否支撑 `daemon_says_absent` 的正向「不存在」回答。**这是本规格唯一可能超出「删文件 + 改接口」范围的风险点。**
- Engram 的 `scope: global` 是否会被 `all_projects=true` 之外的操作意外包含，需确认它对 tier-0 的语义（是否真的跨 project 可见且不污染 project 级召回）。
- Engram `--tools=agent` 的 19 项里，`mem_review` / `mem_judge` / `mem_compare` 是否暴露给主循环，还是只给评审面。属工具面配置，不阻塞规格。

**明确排除在本规格之外**：

- `consciousness` 的任何改动（§5.13）。它的减重是独立子项目，需先做对照分析。
- 交互层（presence 合并、hurry 降级、client_surface 删除）。
