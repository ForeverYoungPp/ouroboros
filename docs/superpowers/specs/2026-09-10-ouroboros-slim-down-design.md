# Ouroboros 精简重构设计：外接 Engram、删除计费、Claudexor 换后端

- 日期：2026-09-10
- 分支：`design/slim-down-engram`
- 状态：待复核

## 0. 一句话

保留 `run_llm_loop` 的形态与「循环自迭代」能力，把记忆整栈换成外接 **Engram**，删掉**计费**与 **Claudexor daemon**，并把 harness 车道的后端从 Claudexor 换成专用 code agent（omp 优先）。

## 1. 背景

现状摸底（只读调查，三份，均有 file:line 取证）得出三条事实：

1. **循环本体很小，围绕它的东西很大。** `ouroboros/loop.py:6087` 的 `run_llm_loop` 只有 296 行（`MAX_FUNCTION_LINES = 300`，`ouroboros/review.py:23`），同文件另有 139 个模块级函数；循环 + 上下文一族合计约 17.6k 行。所谓「自迭代」有四层，只有第①层（轮内多轮 tool-use）在循环内；②自我批判注入在循环内但是 reminder 形态；③任务后 reflection、④Evolution Campaign 都在循环生命周期之外。`run_llm_loop` 在全仓只有 `ouroboros/agent.py:1217` 一个生产调用点。

2. **记忆是叠出来的，不是设计出来的。** 7 类记忆 × 4 套裁剪机制 × 3 套收件箱；工作记忆三写（`scratchpad_blocks.json` + `scratchpad.md` + `scratchpad_journal.jsonl`）、对话压缩带世代游标与分页快照协议、knowledge/patterns/identity 三处都存 old+new 全文副本、`memory/registry.md` 是现场不存在的死机制（`tools/memory_tools.py` + `context.py:1440` 的读取器都指向它）。

3. **交互层有两条并行执行车道。** Web/Telegram 汇入唯一 `LocalChatBridge._inbox`（`supervisor/message_bus.py:154/:302`）走 `server.py:1414 _route_owner_message`；而 presence 绕过 inbox 自建 task/chat_id/gate/Agent（`presence_runner.py:399-419`），8 个文件 2,963 行，终点却是 `agent.handle_task(task)`。

本设计只处理「记忆 / 计费 / Claudexor」三块，交互层的 presence 合并**另案**（见 §2 非目标）。

## 2. 非目标

- **不引入 pi / TypeScript**。pi SDK 只在调研阶段评估过，结论是不采用：其 `Agent`/`agentLoop` 能替换的只有第①层（`while True` + tool dispatch + 事件流），而本循环真实拥有、SDK 没有的机制有 12 项（钱的物理台账、不重发相同字节谓词、wire 投影逐项匹配、发送副本≠canonical transcript、fit/reclaim 三级节拍、轮顶外部权威改写、付费收尾、交付候选+验收绑定、可重入轮、cache_control 锚点、工具执行侧非功能约束、每轮可路由改写）。
- **不重构 `run_llm_loop` 的形态**。轮顶顺序（换路→终止检查→提示注入→压缩→发送）、tool 回填、终止判定保持不变。
- **不动交互层**（presence 合并、hurry 降级、client_surface 删除留作后续独立规格）。
- **不动 supervisor 的任务生命周期**。

## 3. 目标态

```
launcher.py → server.py::_run_supervisor
                 └─ supervisor/{queue,events,task_lifecycle,task_reaper,workers}
                      └─ ouroboros/agent.py:1217 run_llm_loop        ← 形态不变
                           ├─ context.py → context_fit.py（3 段 cache_control 结构不变）
                           │    └─ 内容来源：Engram MCP（不再读本地记忆文件）
                           ├─ llm.py → provider
                           └─ tools/registry.py → 119 个工具

记忆：Engram（Go 单二进制，SQLite+FTS5，~/.engram/engram.db）
     接入缝：ouroboros/mcp_client.py（已存在）→ MCP stdio
计费：不存在
harness 车道：subagent_runtime.py 的 executor=="harness" 分派保留，
             后端从 claudexord 换成 code agent 子进程（omp -p）
保留：ouroboros/context_layout.py 的参考文档章节导航地图
```

## 4. 工作面 A — 记忆外接 Engram

### 4.1 为什么是 Engram

Engram 是 Go 单二进制 + SQLite/FTS5，暴露 CLI / HTTP API / MCP stdio / TUI 四个表面，数据默认在 `~/.engram/engram.db`，环境变量 `ENGRAM_DATA_DIR` / `ENGRAM_PORT` / `ENGRAM_PROJECT`。它按 **project** 分域——与本仓已有的 `data/projects/<id>/` 天然对齐。它的模型是**策展记忆**（observations / session summary / prompt），不是原始日志。

工具面：`mem_current_project` `mem_context` `mem_search` `mem_timeline` `mem_get_observation` `mem_save` `mem_update` `mem_suggest_topic_key` `mem_save_prompt` `mem_session_summary` `mem_session_start` `mem_session_end` `mem_review` `mem_judge` `mem_compare` `mem_doctor`。

### 4.2 接入缝

用**已存在**的 MCP 客户端，不新建传输层：

- `ouroboros/mcp_client.py` 已有 MCP 客户端实现。
- `ouroboros/gateway/mcp.py` 已有管理面（`/api/mcp/status`、`/api/mcp/refresh`、`/api/mcp/test`）。
- 启动命令：`engram mcp --tools=agent`。**必须带 `--tools=agent`**——不带则注册全部 23 个工具，会稀释 agent 的工具面；`--tools=agent` 给 19 个 agent 面向工具。
- 环境：`ENGRAM_DATA_DIR = <DATA_DIR>/engram`。若已有一个 `engram serve` 实例，可用 `ENGRAM_URL` 指向它而不是自行拉起。
- **项目绑定用 `.engram/config.json`**，不是 `ENGRAM_PROJECT`。Engram 的写路径解析顺序是：显式 `project` → 已有 `session_id` → 仓库 `.engram/config.json` / cwd 探测 → 目录名兜底。本仓应在 repo 根放 `.engram/config.json` 写死 `project_name`，对应多项目时用 `data/projects/<id>/.engram/config.json`（Engram 取 **git root 下最近的** config，所以父子项目可各自解析而不互相泄漏）。
- 启动时第一件事调 `mem_current_project` 确认解析结果与 `project_source`，再开始写。这与 ouroboros 既有的「身份按值下传、不从内容反推」原则（`projects_registry.py:196` 的完整性校验）方向一致。

备选（不采用）：HTTP API（`ENGRAM_PORT` / `ENGRAM_URL`）。MCP 更省，因为客户端与管理面都已存在。

### 4.2.1 必须处理的 Engram 错误语义

Engram 的写工具在项目不明确时返回 `ambiguous_project` 并附 `available_projects`，要求调用方**不得猜测**，只能从列表里精确选一个，然后用 `project` + `project_choice_reason: "user_selected_after_ambiguous_project"` 重试。另有 `project_name_collision`（两个名字归一化后落到同一桶）。

本仓在一个 MCP server 下会操作多个项目目录，因此这是**常态路径而非异常路径**。处理要求：`mem_save` 的封装层必须把这个错误透传成 agent 可见的 typed 失败，不能吞掉后降级写入错误项目。Engram 对未背书的名字是 fail-loud 的，本仓不要在其上叠加 fail-open。

### 4.3 工具面替换（对 agent 可见的接口变化）

| 现状 | 换成 | 位置 |
|---|---|---|
| `knowledge_read` / `knowledge_write` / `knowledge_list` | `mem_search` / `mem_save`（带 `topic_key`）/ `mem_get_observation` | `ouroboros/tools/knowledge.py`（420 行） |
| `update_identity` | `mem_save` + `topic_key: self/identity` | `ouroboros/tools/control.py:2245` |
| `update_scratchpad` | `mem_save` + `topic_key: self/scratchpad` | `ouroboros/tools/control.py:2167` |
| `memory_map` / `memory_update_registry` | **直接删**（死机制，现场无 `memory/registry.md`） | `ouroboros/tools/memory_tools.py`（114 行） |

`chat_history` / `recent_tasks` 等读原文的工具**保留**——它们读的是 transcript（`logs/chat.jsonl`），不是记忆。

### 4.4 上下文注入替换

现状：`agent.py:978 → context.py:1558 build_llm_messages → context.py:1341 _capture_context_core` 把记忆拆成 stable / volatile 两段，再经 `context_fit.py:246 _render_context_system_content` 渲染成 3 个 `cache_control` text block。

**保留** `context_fit` 的 3 段结构（static / semi_stable / dynamic）与 `cache_control: ephemeral` 锚点——这是跨任务 prompt cache 的成本机制，不能动。

**替换内容来源**：

| 现函数 | 处理 |
|---|---|
| `context.build_memory_sections`（`context.py:963`） | 改成把一次 `mem_context` 的结果摊进 semi_stable / dynamic 两段 |
| `context.build_knowledge_sections`（`context.py:882`） | 改成 `mem_search`（project 任务走 `ENGRAM_PROJECT`，root 任务走全局） |
| `context.build_recent_sections`（`context.py:1062`） | 保留「未压缩原文尾部」部分（读 `chat.jsonl`），删掉 `task_reflections.jsonl` 渲染 |

`ouroboros/system_projection.py`（把易变字节搬出 system 前缀，145 行）**保留不动**——它与记忆无关。

### 4.5 删除清单

| 文件 | 行数 | 说明 |
|---|---|---|
| `ouroboros/memory.py` | 990 | 工作记忆三写、身份读写、世代读取。全部职能移交 Engram |
| `ouroboros/consciousness.py` | 1386 | daemon 线程。**功能删除**，见 §4.7 |
| `ouroboros/consolidator.py` | 856 | 对话块压缩、世代游标、`[MEMORY GAP]`、knowledge 索引重建 |
| `ouroboros/reflection.py` | 742 | 任务后反思、`MEMORY_ACTIONS_JSON`、Pattern Register 改写 |
| `ouroboros/semantic_dedup.py` | 144 | 唯一的 LLM 语义去重。Engram 的 `mem_suggest_topic_key` / `mem_judge` 接管 |
| `ouroboros/tools/memory_tools.py` | 114 | 死机制 |
| `ouroboros/tools/knowledge.py` | 420 | 替换为 Engram 工具 |

`ouroboros/retention.py`（110 行）**保留**——它只做 worktree / task drive / service log 的 GC 保留天数，与认知记忆无关。

`ouroboros/agent_task_pipeline.py:373-399` 的六步流水线（chat 压缩 → scratchpad 压缩 → task summary → reflection → backlog → promotion）缩成**一次 `mem_session_summary`**，仍然只在 root 任务执行、仍然用 `_is_root_post_task` 的幂等 checkpoint 防重复付费。

### 4.6 保留：章节导航地图

`ouroboros/context_layout.py`（191 行）是参考文档形态的 SSOT（Max 全文 / Low 无损导航图）。消费者：`context.py:52`、`context_fit.py:20`、`deep_self_review.py:27`、`tools/plan_review_runtime.py:98`、`tools/review_subject.py:553`、`tools/scope_review_session.py:53`。

**必须改**：`TIER0_ALWAYS_FULL` 目前硬编码 `{system, bible, identity, scratchpad, knowledge_index, recent_dialogue}`——这四个记忆 section 名在 A 面之后不再存在。改成 Engram 块名（例如 `{system, bible, engram_context, recent_dialogue}`），并同步更新该文件 docstring 里的文档矩阵。

### 4.7 已确认的功能删除（必须记账）

**删除 `consciousness` daemon 后失去两件事**：

1. 「闲时持续认知」——后台线程定时自唤醒（默认 300s，可被 `set_next_wakeup` 工具改到 30–7200s），受 `OUROBOROS_BG_BUDGET_PCT` 约束。
2. 「后台自主改写 identity」——`consciousness.py:1251-1266` 是唯一能在无人指令下改写 `identity.md` 的路径，带 completeness 闸门（`_identity_unresolved_sources` 的 sha256 完整性校验，不完整则返回 `IDENTITY_UPDATE_ABSTAINED`）。

**后果**：identity 从「后台线程自省改写」变成「agent 主动调 `mem_save(topic_key="self/identity")`」。自迭代从 4 层降到 3 层。这是 owner 明确接受的取舍。

**同时失去的连带项**：`_BG_TOOL_WHITELIST` 的 16 项工具白名单机制、`state/consciousness_observations.jsonl` 收件箱协议、`/bg start|stop|status` 命令族（`supervisor/events.py:4070-4078`）、boot 时的 `bg_consciousness_enabled` 自动恢复（`server.py:2208-2216`）、panic 时的 `consciousness.stop()`（`server_control.py:107`）。这些调用点必须逐一清理，不能留悬空引用。

### 4.8 存量数据迁移

现场 `/home/fy/Ouroboros/data/memory/` 的真实内容：

| 文件 | 大小 | 处置 |
|---|---|---|
| `identity.md` | 13.0KB | 一次性导入为 `mem_save(topic_key="self/identity")`，原文归档保留 |
| `identity_journal.jsonl` | 101.3KB | 归档，不导入（只增审计副本） |
| `scratchpad_blocks.json` / `scratchpad.md` | 22.6 / 21.0KB | 导入当前块为 `self/scratchpad`，journal 归档 |
| `scratchpad_journal.jsonl` | 697.9KB | 归档 |
| `knowledge/`（≈47 项）+ `knowledge_history.jsonl` | 580KB | 逐项 `mem_save`，`topic_key` 取原 topic 名 |
| `dialogue_blocks.json` | 33.4KB | 压缩块导入为 observations；`logs/chat.jsonl` 原文不进 Engram（它是 transcript） |
| `WORLD.md` | 662B | 导入为 `topic_key: world` |
| `logs/task_reflections.jsonl` | 2.3MB | 只导入最近 N 条（阈值待定），全量归档 |

迁移脚本是**一次性**的，放在 `scripts/`，执行后保留归档目录不做删除。

## 5. 工作面 B — 删除计费

### 5.1 删除清单

| 文件 | 说明 |
|---|---|
| `ouroboros/usage_accounting.py` | 物理尝试台账、`bind_physical_attempt_context` |
| `ouroboros/usage_ledger.py` | |
| `ouroboros/pricing.py` | |
| `ouroboros/cost_projection.py` | |
| `ouroboros/_usage_response.py` | |
| `ouroboros/_usage_rows.py` | |
| `ouroboros/_usage_rows_memo.py` | |
| `web/modules/costs.js` | 成本页 |
| gateway 的 cost-breakdown 路由 | `gateway/router.py` + `gateway/endpoint_index.py` |

合计约 3,571 行专用文件。

### 5.2 循环内删除（终止出口 8 类 → 7 类）

| 位置 | 机制 |
|---|---|
| `loop.py:247 _check_budget_limits` | 三条预算轴 |
| `loop.py:363 _resolve_task_cost_ceiling` | `task_pacing.resolve_cost_ceiling` |
| `loop.py:425 _soft_land_exhausted_ceiling` | 软着陆 |
| `loop.py:5734 _handle_budget_exceeded` | `BudgetExceeded` 异常路径 |
| `task_pacing.py` 的 `CostCeiling` 三态 | `COST_CEILING_ACTIVE` / `EXHAUSTED_SOFT_LAND` / … |

删除「预算」这**一类**（其下 3 条机制）后剩余的终止出口：

1. 轮数超限（`loop.py:6192` → `_handle_round_limit`）
2. provider 不可用（`loop.py:6314` → `_handle_provider_unavailable`）
3. supervisor `finalize_now`（`loop.py:6218`）
4. 真实 deadline 本地收尾（`loop.py:2832`）
5. 模型自答（`loop.py:6334` → `_no_tool_final_answer`）
6. transport 等待耗尽（`loop_transport.py:248-330`）
7. delegate hold（`loop.py:6207` / `:6308`）

另有两条非循环归属、不受本面影响：派发前的 deadline 拒绝（`loop_llm_call.py:396`，不发车而非终止）与外部上限（supervisor 6h、owner Stop，`loop_transport.py:266-268` 明说不在此复制）。

### 5.3 必须保留

**`ouroboros/context_budget.py` 不要删。** 它是 **token / 字符**预算，与钱无关：`OWNER_LOW_TARGET_TOKENS = 200_000`、`LARGE_CONTEXT_SECTION_CHARS = 200_000`、`MAX_RECENT_CHAT_TAIL = 1000`、`BG_CONTEXT_MAX_CHARS`、`CONTEXT_OVERFLOW_CODES`、`ContextReclaimRequest/Receipt`。消费者：`llm.py:108/236`、`context_fit.py:305/382`、`main_context_authority.py:15`、`agent_startup_checks.py:711/794`、`request_wire_recovery.py:578`。

同理保留 `context_fit.py` 的 `measure_main_fit` 与 reclaim 三级节拍——它们是上下文回收，不是计费。

### 5.4 连带清理

`loop_llm_call.py` 的 `persist_call` 请求 custody、`_normalize_usage_cost`、`add_usage`、`fold_retrieval_usage`、`emit_llm_usage_event` 中，**只删钱的部分**（cost/pricing），**保留 token 计数**——`context_fit` 与 `llm.py` 的上下文判定依赖它。

## 6. 工作面 C — Claudexor daemon → code agent harness

### 6.1 删除清单（约 4,821 行）

| 文件 | 行数 |
|---|---|
| `ouroboros/claudexor_daemon.py` | 978 |
| `ouroboros/claudexor_runtime.py` | 1,559 |
| `ouroboros/gateways/claudexor.py` | 933 |
| `ouroboros/gateway/claudexor_accounts.py` | 999 |
| `ouroboros/gateway/claudexor_quota.py` | 41 |
| `ouroboros/claudexor_runtime_pin.json` | Node 24.16.0 逐平台 sha256 钉定 |

Web 侧**没有纯 harness 模块**——实测全部是混合文件，只能删其中的 harness/账号分支，不能整文件删：

| 文件 | 总行数 | 命中 harness/账号的行 |
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

因此 §5 的「净删行数」只对 Python 侧成立；web 侧是**逐分支删改**，需要在每个文件内用 `lsp`/grep 核对调用点后手动切除，并同步 `web/tests/` 下对应的 12 个测试与 fixture。这一步的工作量按「改动点」而非「删除文件」估算。

### 6.2 保留的车道

`subagent_runtime.py:237-239` 的 `executor` 分派是本面**要保住**的抽象：

```
executor == "harness"  →  外部 code agent（原 Claudexor，现换后端）
executor == "native"   →  自己的 subagent 循环
auto                   →  :288-289 的 legacy 解析
```

`:460` 的 `executor = "blocked" if unavailable else "harness"` 与 `:399/:408/:472` 的 `requested_executor` / `effective_executor` 报账结构保持。

`delegate_custody.py`、`review_execution.py`、`delegate_hold.py`、`delegate_containment.py`、`delegate_recovery.py` 里的 harness 语义保留，只替换后端调用点（`:1130/:1352/:1360/:1396/:1497`、`review_execution.py:653-654/968/1251`）。

### 6.3 语义落差（本面主要工作量）

**Claudexor 是长驻 daemon**：socket + `/v2` 控制 API + 账号授权 + 并发会话 + 设备码登录流程。

**code agent 是一次性子进程**：omp 的 headless 面是 `omp -p "<prompt>"`（`--print` / `--print-thoughts` / `--prompt-cache-key`，见 omp `docs/cli-reference.md:39,163-190`）。

因此 harness 抽象要从「长驻会话」降级为「每次委派一个子进程 + 自管 `--prompt-cache-key`」。这**不是 drop-in 替换**：

- `delegate_custody.py` 里依赖 daemon 会话连续性的假设需要重新设计（原 daemon 复用同一进程与上下文，子进程每次冷启动）。
- 并发控制从 daemon 自带改为本仓自己管（`model_concurrency.py` 已有 `model_call_slot`，可复用）。
- 登录态/授权 UI 整块消失——账号由 code agent 自己持有，本仓不再托管凭据（这与 `claudexor_daemon.py` docstring 里「Zero auth logic lives here」的既有立场一致，延续而非倒退）。
- `claudexor_runtime_pin.json` 的「钉定 Node 运行时 + 校验 sha256」机制需要等价物：钉定 code agent 的版本与其启动方式。

## 7. 工作面 D — 循环

**不改形态**。`run_llm_loop`（`loop.py:6087`，296 行）的轮顶顺序、tool 回填、终止判定保持原样。

**顺带做减法**（利用棘轮的单向性）：LoopScout 判定为「纯行数/字节规避」的抽取物，在 A/B/C 触碰到的范围内合回：

| 抽取物 | 证据 | 判定 |
|---|---|---|
| `_account_compaction_usage`（`loop.py:2631`） | 自陈「for the 300-line function gate」 | 纯行数规避，真实逻辑 3 行 |
| `_force_plan_decision/reminder/disclosure`（`loop.py:196/216/222`） | `owner_hurry.py:455` 自陈「byte-neutral extraction for the pinned loop.py」 | 文本外移 |
| `context.py:_project_room_fact`(325) / `_OWNER_CLIENT_NOTE`(311) | 自陈「to keep that builder under the hard method gate」 | 文本外移 |

`loop.py` 的 `BYTE_DEBT = 284435`（不可变基线 `BYTE_BASELINE_DEBT = 335600`）**只能缩不能增**，所以这批是机会不是负担。

**保留**：`loop_transport.py`（真边界：provider 降级节拍）、`delivery_protocol.py`（真边界：协议解析器）、`acceptance_dialogue.py`（真边界，loop 刻意 re-export 以保单一 import 面）、`empty_round_guard.py`。

## 8. 执行顺序

依赖顺序，不可并行合并成一个批次：

1. **B 删计费** — 耦合最低；删完循环的终止路径更清楚，为 A/C 减少噪音。
2. **C 换 harness 后端** — 与记忆完全独立，可独立验证（跑一次 delegate 与一次 review）。
3. **A 接 Engram + 删自研记忆** — 最大，且依赖前两步腾出的干净上下文；末段需要一次性的存量迁移（§4.8）。

## 9. 风险与对策

| 风险 | 证据 | 对策 |
|---|---|---|
| `size_ratchet` CI lane 阻塞 | `GIANT_PATHS` 含 `loop.py` `llm.py` `server.py` `supervisor/workers.py` `tools/control.py`（`size_ratchet_manifest.py`），48 条路径精确记账 | 删除是棘轮允许方向；但 `BYTE_DEBT` 必须与文件在**同一次 commit** 移动；用 `scripts/regenerate_size_ratchet.py` 重新生成而不是手改 |
| 测试直接 import 私有符号 | `_drain_incoming_messages`（`test_available_subagents_core_followup.py:312`）、`_check_budget_limits`（`test_budget_limits.py:10`）、`_maybe_inject_finalization_nudges`（`test_delegation_phase_b.py:301`）、`seal_task_transcript`（`test_anthropic_empty_block_fix.py:13`） | 下划线前缀在本仓是名义上的。删除前用 `lsp references` 逐个核对，删除即改测试 |
| 621 个测试文件的存量 | `tests/` 621 文件 / 334,189 行，大于源码总量 | 每个工作面完成即跑该面相关测试，不跑全量到最后 |
| `tier-0` 与记忆 section 名硬耦合 | `context_layout.py` 的 `TIER0_ALWAYS_FULL` | 与 A 面同批改，docstring 文档矩阵同步 |
| 存量记忆数据丢失 | `~/Ouroboros/data/memory/` 约 1.1MB 活跃数据 | §4.8 迁移脚本 + 归档不删；迁移失败可回滚（Engram 是独立 SQLite，删库重来即可） |
| `consciousness` 悬空引用 | 调用点分布在 `server.py:1597/1607/1868-1878/2208-2216`、`server_control.py:107`、`supervisor/events.py:4070-4078`、`gateway/*` | 删除时逐点清理，`/bg` 命令族一并移除并在文档中记账 |
| Engram 可用性 | 外部二进制依赖 | 二进制缺失时 fail-fast 并给出明确错误；不静默降级成「无记忆」（会退化成每次冷启动） |
| Engram 项目解析歧义 | 本仓在一个 MCP server 下操作多个项目目录，`ambiguous_project` 是常态路径 | §4.2.1：透传 typed 失败，禁止吞错误后降级写错项目 |
| 身份语义漂移 | identity 从「后台自省改写」变成普通 topic，且被 Engram 的 project 分域切开 | 若要保身份连续性，需在 §4.8 迁移时选定一个 account 级 project（例如固定的 `ouroboros-self`）承载 `self/*` topic，而不是散落到各 project |

## 10. 验证策略

**A 面（记忆）**
- 现象验证：一次会话内 `mem_save` 一条知识 → 重启运行时 → 新会话 `mem_search` 能取回。
- identity 往返：`mem_save(topic_key="self/identity")` → 下一次 `build_llm_messages` 的 semi_stable 块里出现该内容。
- 迁移验证：迁移脚本跑完后，`mem_search` 能命中迁移前 `knowledge/` 里的一个已知 topic。
- 反例验证：切断 Engram（改名二进制）→ 运行时给出明确错误而不是空记忆启动。

**B 面（计费）**
- 一次正常任务跑完，`run_llm_loop` 不抛 `BudgetExceeded`，且 `gw` 的 cost-breakdown 路由返回 404（已删）。
- 终止出口回归：逐条触发 5 类剩余出口，确认仍能产出可交付答案。

**C 面（harness）**
- 一次 `delegate` 走 `executor=="harness"` 成功产出结果；一次 review 同上。
- `executor=="native"` 车道不受影响（回归）。
- code agent 缺失时 `executor=="blocked"` 且报账结构完整（`:399/:408`）。

**D 面（循环）**
- 一份冒烟任务：提交 → 多轮 tool-use → 收尾，行为与重构前一致。
- `size_ratchet` lane 通过。

## 11. 已确认的取舍台账

| 取舍 | 内容 | 后果 |
|---|---|---|
| 记忆全交 Engram | identity 也做成 `topic_key` | Engram 按 project 分域，身份会被 project 切开 |
| 删 consciousness | 失去闲时持续认知 + 后台自主改写 identity | 自迭代 4 层 → 3 层 |
| 删计费 | 失去预算硬护栏 | 终止出口 8 类 → 7 类；`context_budget.py` 不受影响 |
| 砍 Claudexor daemon | 失去 daemon 会话连续性与托管登录态 | harness 降级为一次性子进程；授权由 code agent 自持 |
| 不采用 pi | 循环留在 Python | 循环形态不变；12 项治理不需要跨语言重建 |

## 12. 待定项

- §4.8 中 `task_reflections.jsonl` 的导入阈值（「最近 N 条」的 N）。
- §6.3 code agent 的**首发后端具体是哪个**（omp 已确认是目标，但需确认本仓是否需要同时支持多种 code agent 的探测/选择，以及钉定版本的等价物形态）。
- §9「身份语义漂移」一行里的 account 级 project 名（建议 `ouroboros-self`）是否采用。
- Engram MCP 工具面已确定用 `--tools=agent`（19 个，见 §4.2），但**具体哪几个进 ouroboros 的 agent 工具面**还需裁剪（例如 `mem_review` / `mem_judge` / `mem_compare` 是否暴露给主循环，还是只给评审面）。
