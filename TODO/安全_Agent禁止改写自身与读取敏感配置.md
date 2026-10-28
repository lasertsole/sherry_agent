# 安全需求:禁止 Agent 改写自身源码与读取敏感配置

> 结论先行:
> 1. **写工具(`write_file`/`patch_file`)与 shell 类工具(`terminal`/`python_repl`/`ptc`)
>    对 sherry 自身文件采用白名单策略:默认全禁,仅"与运行时无关"的特定目录
>    (`.sherry/`、当前会话产物目录、`logs/`/`temp/` 等)可写,其余(源码树、运行时
>    状态、配置文件)一律拒绝**,除非用户显式 HITL 授权。
> 2. **读工具(`read_file`/`search_files`)与 shell 类工具对 sherry 自身文件采用白名单
>    策略:默认全禁,仅"读了不泄露"的特定目录(`.sherry/`、当前会话 `plans/`、`TEMP_DIR`、
>    上传媒体区等)可读,其余(源码树、配置与凭证、运行时状态、用户私人记忆、会话内容)
>    一律拒绝**,除非用户显式 HITL 授权。读取白名单比写入白名单更窄(排除含历史对话的
>    `sessions/<sid>/evicted/` 等)。
> 3. 当前两者**均无代码层强制**:`resolve_external_path` 的 1a 快速通道把整棵 `ROOT_DIR`
>    标为"仓库内即安全"直接放行,PathGuard 的 in-root 策略不再查 hard-deny floor,
>    且默认未配置时会话项目目录就回退到 `ROOT_DIR` 自身——详见
>    [计划_子代理并发写防护与符号破坏检测.md](计划_子代理并发写防护与符号破坏检测.md)
>    与上一轮「sherry 自改自身风险」调研。

---

## 一、背景与缺口(为什么要做)

### 1.1 自改自身路径已通,无审批

| 缺口 | 位置 | 现状 |
| --- | --- | --- |
| 仓库内即安全快速通道 | `agent/tools/pub_base/path_utils.py:617-619` | `resolved.is_relative_to(ROOT_DIR)` 直接返回,**跳过 YOLO/allowlist/HITL 全部分支** |
| 默认项目目录回退 checkout | `config/path.py:488-494`、`runtime/session/project_dir.py:156-164` | `SHERRY_PROJECT_DIR` / `sherry.jsonc` 的 `project_dir` 都空时 → `ROOT_DIR`,开箱即用即指向自身源码树 |
| PathGuard in-root 放行 | `agent/middlewares/path_guard/core.py:106-109` | "in-root passes, outside the root only the hard-deny floor rejects"——root 内**根本不查** floor |
| hard-deny floor 不含自身 | `path_guard/core.py:71`、`config/features/agent_side/hitl_defaults.py:39-48` | floor 仅 `/etc/passwd`/`/etc/shadow`/`/etc/sudoers` + `~/.ssh`/`~/.aws`/`~/.gnupg`/`~/.env` 等外部凭证 |
| `validate_project_dir` 不拦 | `config/path.py:447-471` | 只校验绝对/存在/是目录/非符号链接环,**不拒绝绑定到 `ROOT_DIR`** |
| shell 写完全绕过文件许可 | `agent/tools/...` 见 [计划_子代理并发写防护与符号破坏检测.md](计划_子代理并发写防护与符号破坏检测.md) §1.3 | `terminal`/`python_repl`/`ptc` 写文件无许可、无 CAS、无 `path_lock`,证据记账也不挂 |

**判定:agent 在默认配置下可在无 HITL 审批下 `patch_file` 修改 `agent/core.py` 这类自身源码,
也可 `cat` / `python -c "open(...).write(...)"` 走 shell 改写。** `patch_file` 还不要求事先读
(`patch_file.py:220` 自读自写),零前置即可改源码。`shadow_git` 快照是事后回滚手段,
不是事前拦截。

### 1.2 敏感配置读取路径已通,无审批

`.models`(provider/endpoint/**key**/local-model switch,gitignored)、
`.network`(`TAVILY_API_KEY`/`SEARXNG_*`/`SHERRY_BROWSER_*`/`LANGSMITH_*`,gitignored)、
`src/data/auth.db`(scrypt 密码 hash + JWT 黑名单)、`workspace/memory/`(用户私人记忆
`MEMORY.md`/`USER.md`/`FACTS.md`)等同样落在 `ROOT_DIR` 内(或 `APP_HOME == ROOT_DIR` 的
checkout 模式下落在 checkout 内),1a 快速通道与 in-root 策略一并放行。`read_file` /
`search_files` 经 `resolve_tool_path`(`path_utils.py:454-478`)同样命中 1a。

YOLO deny list 里的 `~/.env` 只覆盖**用户家目录**下的 `.env`,不覆盖项目根的 `.env`;
`auth.db`、`.models`、`.network`、`workspace/memory/` 完全不在任何黑名单内。

**判定:agent 可在无审批下读取自己的 API key、用户密码 hash、用户私人记忆。** 泄露面包括
把 key 写进对话发给远端、写进 tool result 被持久化、被记忆系统收录后跨会话泄露。

---

## 二、需求 1:写工具与 shell 类工具对 sherry 自身采用写入白名单

### 2.1 覆盖工具(必须全部,缺一即绕过)

| 工具 | 写路径 | 备注 |
| --- | --- | --- |
| `write_file` | `agent/tools/file_tools/write_file.py` | 覆盖需读许可,但 append 模式不要(`write_file.py:323-330`) |
| `patch_file` | `agent/tools/file_tools/patch_file.py` | **不要求事先读**,零前置可改 |
| `terminal` | shell 重定向/`sed -i`/`>`/`>>` | 绕过所有文件许可 |
| `python_repl` | `open(...,'w')`/`Path.write_text` | 同上 |
| `ptc` | code-intel runner | 同上 |
| 回放门 formatter | `regression_gate.py:43-74` 白名单(`black`/`prettier`/`ruff`) | 一次性直写,绕过许可——**需单独评估是否纳入**(见 §六) |

### 2.2 写入白名单:default-deny,仅"与运行时无关"的目录可写

策略从黑名单改为**白名单**:agent 用 `write_file`/`patch_file` 写 sherry 自身文件时,
默认全部拒绝,只有下列"与运行时无关"的目录可写(改了不破坏 sherry 进程运行、不影响
其他会话、不泄露凭证)。

> **不误伤前提(已核实)**:白名单挂在 `resolve_external_path` 1a 之后,而该函数只被
> file_tools 四件套(`write_file`/`patch_file`/`read_file`/`search_files`)经
> `resolve_tool_path` 调用(`write_file.py:182`、`patch_file.py:145`、`read_file.py:205`、
> `search_files.py:222`)。agent 的内部功能写入——`memory` 工具写 `workspace/memory/`
> 三件套、`skill_manage` 写 `AUTO_SKILLS_DIR`、`exit_plan_mode` 写 `sessions/<sid>/plans/`、
> evidence ledger / shadow_git / checkpointer / heartbeat / trajectory / knowledge 索引等——
> 都走独立工具或 sherry 进程路径,直接用 `config.path` 常量拼路径,**不经过
> `resolve_external_path`**。所以白名单只管"agent 用通用文件工具写 sherry 自身"这一面,
> 不会拦截 agent 自身功能。

**判定边界**:路径落在 `ROOT_DIR` 或 `APP_HOME` 内即视为"sherry 自身文件",走白名单
判定;落在用户绑定的会话项目目录内(且该目录 ≠ `ROOT_DIR`)属"用户工作区",沿用
既有 in-root 放行 + 外部审批,不受本白名单约束。默认未配置时项目目录 == `ROOT_DIR`,
此时白名单是唯一可写面——堵住"默认指向自身源码"的风险(见 §1.1)。**注意:此效果依赖
白名单挂点在 `resolve_tool_path` 而非仅 `resolve_external_path`——后者在默认配置
(root==ROOT_DIR)下不被触达,详见 §2.4 与 §五方向 B。**

**写白名单判定规则**:改了不破坏 sherry 进程运行、不影响其他会话、不泄露凭证 → 可写。
读白名单判定规则见 §3.2(比写更窄:源码树可读,因为读不破坏运行时)。

**白名单(可写,被 `write_file` 改了无害)**:

| 目录 | 路径定义 | 为什么可写 |
| --- | --- | --- |
| 用户项目数据目录 | `<project_dir>/.sherry/`(`shadow_git.py:60`、`code_intel/roots.py:35`) | codeintel 索引、shadow-git 快照、subagent attachments,本就是 agent/sherry 写入面 |
| 当前会话产物(收窄) | `SESSIONS_DIR/<当前 sid>/` 的 plans / attachments / uploads(`config/path.py:204`) | 当前会话自己的产物,只影响本会话;**仅当前会话,跨会话写禁止**;`evicted/` 已剔除(保护事实完整性) |
| 临时文件 | `TEMP_DIR` = `APP_HOME/temp`(`config/path.py:124`) | ptc runner、subagent attachments 默认根,每调用隔离 |
| 生成静态资源 | `STATIC_DIR` = `APP_HOME/static`(`config/path.py:123`) | `text_to_image` 等 skill 写 PNG,无运行时影响 |
| 上传媒体区 | `SRC_DIR/images`、`SRC_DIR/audio`、`SRC_DIR/video`(`server/__main__.py:294`) | 用户上传媒体静态服务区 |
| RAG 上传暂存 | `SRC_DIR/rag/uploads/`(`knowledge_graph.py:58`) | RAG 文档摄取暂存 |

> `SESSIONS_DIR/<当前 sid>/` 已从整目录**收窄**为 plans / attachments / uploads:`evicted/`
> (淘汰历史消息预览,`eviction.py:126`)含历史对话片段,写它会破坏事实完整性,已移出
> 白名单单独保护(默认禁)。白名单不得包含任何会话历史内容,以免 agent 篡改自身记录。

**白名单外(默认禁)**:

```
# sherry 自身源码树(全部,无例外)
agent/ server/ config/ runtime/ pub/ context_engine/
models/                # 注意:checkout 下与 MODELS_DIR 重合,含源码+权重,整树禁
workspace/template/    # shipped persona 种子(只读)
workspace/*.py         # file_sync.py / prompt_builder.py / __init__.py(受跟踪产品代码)
skills/builtin/        # shipped builtin 技能(只读)
plugins/ channels/ bus/    # 内置 plugins(channels、mcp_server/config.json 默认)
tests/ scripts/ packaging/ evals/ client/ wiki/

# sherry 运行时状态与凭证(改了破坏运行时或泄露)
workspace/AGENTS.md  SOUL.md  USER.md  ROLE.md  IDENTITY.md   # 根 persona,用户经客户端面板写
workspace/memory/{MEMORY,USER,FACTS}.md                    # 走 memory 工具(有注入扫描),write_file 禁
workspace/knowledge/                                       # sherry 进程写(plans/index)
MODELS_DIR(APP_HOME/models)                               # 模型权重,改坏影响推理
MCP_SERVERS_DIR(APP_HOME/mcp_servers)                     # 可执行命令配置,agent 不应代写
USER_SKILLS_DIR / AUTO_SKILLS_DIR / PLUGIN_SKILLS_DIR     # 走 skill_manage 工具
SRC_DIR/data/*                                            # boulder.json / evidence-ledger.db / approvals.json / locks / isolated
SRC_DIR/data/auth.db                                      # scrypt 密码 hash + JWT 黑名单
SRC_DIR/checkpoints/sqlite.db                             # LangGraph 会话状态/消息历史
SESSIONS_DIR/trajectory.db                                # 共享 trajectory,跨会话
SESSIONS_DIR/<当前 sid>/evicted/                          # 淘汰历史消息,保护事实完整性
SESSIONS_DIR/<其他 sid>/                                  # 跨会话隔离
.models  .network  .env  sherry.jsonc                     # 配置与凭证
CODE_INTEL_DIR  LOG_DIR                                   # sherry 自写(符号索引/日志)
```

**豁免(sherry 进程内部写入面,不经过 file_tools,白名单不影响)**:
`workspace/HEARTBEAT.md`、`MEMORY_INDEX_DIR`、`KNOWLEDGE_INDEX_DIR`、`PLAN_KNOWLEDGE_DIR`、
`SKILLS_STATE_FILE`、`SKILLS_SNAPSHOT_FILE`、`sessions/<sid>/evicted/`(sherry 中间件写)、
`SRC_DIR/data/{boulder.json,evidence-ledger.db,approvals.json,locks,isolated}`——这些是
sherry 自己的写入,不经 `resolve_external_path`,白名单挂上后对它们无影响,仅作记录。

> 注:`logs/`(`LOG_DIR`)从初版白名单移到默认禁:sherry logger 进程写,agent 用
> `write_file` 写日志无业务意义,且日志可能含对话片段,默认禁更稳;logger 自己写不经
> file_tools,不受影响。

### 2.3 白名单内放行 / 白名单外 HITL

- **白名单内的写入直接放行,不需要 HITL**——这些目录是 main agent 正常功能的写入面
  (plan 落盘、临时产物、上传托管等),若每次都走 HITL 会打断 main agent 正常工作流。
- 白名单外的写入默认**拒绝**(hard-deny,不可 YOLO 跳过)。
- 用户在 HITL 卡片上**显式批准**(approve,需带"我理解这会修改 sherry 自身文件"语义)
  后放行单次写入,批准不持久化为会话级 allowlist(下一次仍需审批)。
- 不走 YOLO 全局开关、不走会话 allowlist——这两者本就是为外部文件设的便利通道,
  对 sherry 自身文件不适用。

### 2.4 实现位置(待选,见 §五)

**关键:`resolve_external_path` 的 1a 快速通道不是覆盖默认配置的挂点。** 默认配置下
session root == `ROOT_DIR`,`resolve_tool_path`(`path_utils.py:454-478`)先走
`resolve_workspace_path`(in-root gate),路径 `is_relative_to(ROOT_DIR)` 即直接返回——
**不调 `resolve_external_path`**。因此白名单必须挂在 `resolve_tool_path` 的
`resolve_workspace_path` 返回之后(覆盖默认配置 root==ROOT_DIR),同时在
`resolve_external_path` 的 1a 之后也挂(覆盖非默认配置下绝对路径指向 ROOT_DIR 内的场景)。
`search_files` 直接调 `resolve_external_path` 不经 `resolve_tool_path`,需在它的 resolve
后加同一判定,或把判定逻辑抽成共享函数两处都调。

shell 类工具的拦截:把"白名单外自身路径"加进 PathGuard 的 hard-deny floor,
`_screen_command_paths`(`path_guard/core.py:135-161`)天然查 floor,不分读写(简单且与
文件工具拒绝语义一致)。subagent 隔离黑名单(§四)的实现见 §4.5。

---

## 三、需求 2:读工具与 shell 类工具对 sherry 自身采用读取白名单

### 3.1 覆盖工具

| 工具 | 读路径 | 备注 |
| --- | --- | --- |
| `read_file` | `agent/tools/file_tools/read_file.py` | 经 `resolve_tool_path` 命中 1a |
| `search_files` | `agent/tools/file_tools/search_files.py` | 内联同一 resolve 模式(`search_files.py:217-226`) |
| `terminal` | `cat`/`head`/`grep`/`less`/重定向读 | 绕过文件许可 |
| `python_repl` | `open(...)`/`Path.read_text` | 同上 |
| `ptc` | code-intel runner | 同上 |

### 3.2 读取白名单:default-deny,仅"读了不泄露"的目录可读

策略从黑名单改为**白名单**:agent 用 `read_file`/`search_files` 读 sherry 自身文件时,
默认全部拒绝,只有下列"读了不泄露凭证/隐私/会话内容"的目录可读。

**读白名单判定规则**:读了不泄露凭证/隐私/会话内容 → 可读。比写白名单(§2.2)更窄:
源码树可读(读不破坏运行时,写会);有些目录写了无害但读了会泄露(最典型
`sessions/<sid>/evicted/` 含历史对话),这类目录在写白名单内、在读白名单外。

**读取白名单(可读)**:

| 目录 | 路径定义 | 为什么可读 |
| --- | --- | --- |
| sherry 自身源码树 | `agent/` `server/` `config/` `runtime/` `pub/` `context_engine/` `models/` `workspace/template/` `workspace/*.py` `skills/builtin/` `plugins/` `channels/` `bus/` `tests/` `scripts/` `packaging/` `evals/` `client/` `wiki/` | 读源码不破坏运行时;agent 理解自身/调试时读源码是合理操作 |
| 用户项目数据目录 | `<project_dir>/.sherry/` | codeintel 索引、shadow-git 快照、attachments;agent 本就能读用户项目代码,读这些不增加泄露面 |
| 当前会话计划 | `SESSIONS_DIR/<当前 sid>/plans/`(`config/path.py:366`) | agent 自己的计划,本会话隔离;**仅 plans/,不含 evicted/** |
| 临时文件 | `TEMP_DIR`(`APP_HOME/temp`) | agent 自己写的临时产物 |
| 生成静态资源 | `STATIC_DIR`(`APP_HOME/static`) | 生成的图片等,无敏感 |
| 上传媒体区 | `SRC_DIR/images`、`SRC_DIR/audio`、`SRC_DIR/video` | 用户上传媒体,agent 本就能看 |
| RAG 上传暂存 | `SRC_DIR/rag/uploads/` | RAG 文档摄取暂存 |

> **与写白名单的差异**:读白名单额外放行**自身源码树**(读源码不破坏运行时,写源码会);
> `SESSIONS_DIR/<当前 sid>/` 在写白名单里是 plans/attachments/uploads,在读白名单里收窄为
> 仅 `plans/`——`evicted/`(淘汰历史消息)写读都禁(保护事实完整性)。

**读白名单外(默认禁,需 HITL)**:

```
# 凭证与配置(读了直接泄露)
.models  .network  .env  sherry.jsonc
.models.example  .network.example    # 暴露配置拓扑,建议一并禁读
MCP_SERVERS_DIR(APP_HOME/mcp_servers/*.json)   # 含 token/命令

# 运行时状态与持久数据(读了泄露会话/隐私)
SRC_DIR/data/auth.db            # scrypt 密码 hash + JWT 黑名单(auth_store.py:40)
SRC_DIR/data/approvals.json     # 工具审批记录(config/path.py:524)
SRC_DIR/data/evidence-ledger.db # 跨会话验证证据(含命令输出,config/path.py:438)
SRC_DIR/checkpoints/sqlite.db   # LangGraph 会话消息历史/状态(async_sqlite_checkpointer.py:14)
SESSIONS_DIR/trajectory.db      # 共享 trajectory(跨会话,trajectory_store.py:29)
sessions/<id>/evicted/          # 淘汰的历史消息预览(eviction.py:126)
sessions/<其他 sid>/            # 跨会话隔离
workspace/memory/{MEMORY,USER,FACTS}.md   # 用户私人记忆
workspace/knowledge/            # 知识库(config/path.py:192)
MODELS_DIR                      # 模型权重,读无意义,默认禁
USER_SKILLS_DIR / AUTO_SKILLS_DIR / PLUGIN_SKILLS_DIR   # 技能定义,保守禁
CODE_INTEL_DIR  LOG_DIR         # sherry 自写(日志含对话片段)
```

**注意**:`src/data/litellm_prices.json`(价格缓存,无密)不含凭证,按白名单语义它落在
`SRC_DIR/data/` 下默认禁,如需放行可单独加白名单。agent 自身的记忆系统
(`context_engine`)直接读 MesMemory SQLite 而非读 `workspace/memory/*.md`,所以禁读
这些文件**不会破坏记忆功能**,只阻止 agent 把用户私人记忆原文喂进对话/工具结果。

### 3.3 白名单内放行 / 白名单外 HITL

- **白名单内的读取直接放行,不需要 HITL**——main agent 读自身源码、临时产物等是正常行为。
- 白名单外的读取默认**拒绝**(hard-deny,不可 YOLO 跳过)。
- 用户显式批准后放行单次读取,不持久化。
- 与需求 1 同理,不走 YOLO/allowlist 便利通道。

### 3.4 实现位置

同 §2.4,加一份"读取白名单"路径集合,在 `resolve_external_path` 与 PathGuard
`_screen_command_paths` 各加一道判定。**读取白名单与写入白名单是两个独立集合**:
写入白名单按"改了无害"判定、读取白名单按"读了不泄露"判定(更窄,排除 `evicted/` 等
含历史对话的子树),不要合并。两份白名单都挂在 `resolve_external_path` 1a 之后,对
file_tools 四件套生效;agent 内部读取(`context_engine` 读 MesMemory、codeintel 读
索引等)走独立路径,不受影响。

---

## 四、需求 3:subagent 在白名单基础上叠加隔离黑名单

subagent(子会话)继承 main agent 的写白名单与读白名单(§2.2 / §3.2),但在其上**额外
叠加一层黑名单**:禁止读写 main agent 与其他 subagent 的历史记录及其 workspace 目录,
确保子代理不能窥探或篡改父/兄弟会话。

### 4.1 覆盖工具

同 §2.1 + §3.1(`write_file`/`patch_file`/`read_file`/`search_files` + `terminal`/`python_repl`/`ptc`)。

### 4.2 隔离黑名单(无论读写,一律禁止,不走 HITL)

| 目标 | 说明 |
| --- | --- |
| main agent 的会话目录 | `SESSIONS_DIR/<main sid>/`(plans/attachments/evicted/uploads)——子代理不得读写父会话任何产物 |
| 其他 subagent 的会话目录 | `SESSIONS_DIR/<sibling sid>/`——不得跨子代理窥探 |
| main agent 的 workspace 目录 | main agent 绑定的项目目录及其 `.sherry/`(子代理自己的隔离工作树除外) |
| 其他 subagent 的隔离工作树 | `SRC_DIR/data/isolated/<...>`、sibling 的 `<child_root>/.sherry/attachments/` |
| 跨会话持久状态 | `checkpoints/sqlite.db`、`trajectory.db`、`evidence-ledger.db`、`approvals.json`(本就在读写白名单外,对 subagent 重申) |

### 4.3 与白名单的关系

- subagent 的**白名单**与 main agent 相同(§2.2/§3.2),但白名单内路径若**落在隔离黑名单**
  范围(如某 sibling subagent 的 `SESSIONS_DIR/<sibling sid>/plans/`——plans 虽在白名单内,
  但属其他 subagent),黑名单优先,**拒绝**。
- 即:subagent 的可写/可读 = (main 白名单) ∩ (非隔离黑名单)。
- subagent 自己的隔离工作树(`SRC_DIR/data/isolated/` 或 `<child_root>/.sherry/`)是它**自己**
  的写入面,不在隔离黑名单内(写自己的工作树正常)。

### 4.4 例外

- 用户显式 HITL 授权可放行单次(同 §2.3/§3.3)。
- subagent 完成合并(`workspace_merge.py`)写回 parent 项目树是既有流程,不在本黑名单禁止
  范围(但应触发写白名单审批,见 §六.3)。

### 4.5 实现位置

隔离黑名单是 subagent **专属**的一层,挂在 subagent 的工具调用路径上(通过 session 身份
判断:当 `session_id` 属 subagent 时叠加判定)。可在 `resolve_external_path` 增加
`is_subagent` 分支:subagent 身份下,路径落在隔离黑名单 → 拒绝(不走 HITL,跨会话隔离是
硬约束);或 PathGuard 的 `_screen_path_arg` 在 subagent 上下文下加一道黑名单检查。shell
类工具(terminal/python_repl)同样需在 subagent 身份下叠加黑名单扫描(同 §五方向 C)。

---

## 五、实现方向(三选一或组合,待评审)

### 方向 A:扩展 hard-deny floor + in-root 也查 floor(最小侵入)

- `_is_hard_denied`(`path_guard/core.py:94-100`)增加"sherry 自身路径但不在写/读白名单内"
  的判定(写白名单见 §2.2、读白名单见 §3.2)。
- 修改 in-root 策略(`core.py:106-109`):in-root 不再无条件放行,改为"仍查 floor"。
  **风险**:会改变 PathGuard 对所有 in-root 写的既有语义,需回归测试覆盖。
- 优点:单点修改,shell 与文件工具统一过 floor。
- 缺点:floor 原本是"系统凭证"语义,扩展后语义变宽;读/写需分两个 floor 谓词(写白名单外、读白名单外)。

### 方向 B:白名单判定挂在 `resolve_tool_path` + `resolve_external_path`(推荐)

- **两处都挂**:(1) `resolve_tool_path`(`path_utils.py:454-478`)的 `resolve_workspace_path`
  返回之后——覆盖默认配置(root==ROOT_DIR,路径在 in-root 阶段放行,不走
  `resolve_external_path`);(2) `resolve_external_path` 的 1a(`path_utils.py:617-619`)
  之后——覆盖非默认配置下绝对路径指向 ROOT_DIR 内的场景。`is_sherry_self_path(resolved)`
  命中(落在 `ROOT_DIR`/`APP_HOME` 内)且不在写/读白名单 → 走 HITL `interrupt`
  (复用 `_external_access_hitl`,`path_utils.py:573-584`),不走 YOLO/allowlist。
  `search_files` 直接调 `resolve_external_path` 不经 `resolve_tool_path`,需在它的
  resolve 后加同一判定,或抽共享函数。
- **原方案(仅 1a 后挂)的致命盲区**:默认配置下 root==ROOT_DIR,`agent/core.py` 走
  `resolve_workspace_path` 的 in-root 放行,不调 `resolve_external_path`,1a 挂点不触达。
  §2.2「默认配置下白名单是唯一可写面」的断言依赖挂点前移到 `resolve_tool_path`。
- **已核实不误伤 agent 内部功能**(见 §2.2「不误伤前提」):`resolve_tool_path`/
  `resolve_external_path` 只被 file_tools 四件套调用,memory/skill_manage/exit_plan_mode/
  evidence/shadow_git/checkpointer 等内部写入走独立路径,白名单零影响。
- 优点:语义清晰(白名单内放行、白名单外自身路径走审批),不影响外部文件既有审批流;
  HITL 卡片可直接渲染"这会修改 sherry 自身文件 / 读取 API key"的警示文案。
  能走 HITL 审批(方向 A 只能 hard-deny)。
- 缺点:`resolve_external_path`/`resolve_tool_path` 只覆盖文件工具路径,**shell 类工具
  不经过它**(terminal/python_repl 直接 spawn)——需配合方向 C(floor 扩展)。

### 方向 C:扩展 hard-deny floor,shell 天然查(必须配套)

- 把"白名单外 sherry 自身路径"加进 PathGuard 的 hard-deny floor(`_is_hard_denied`,
  `core.py:94-100`),`_screen_command_paths`(`core.py:135-161`)天然查 floor,**不分读写**
  (简单,且与文件工具的拒绝语义一致;区分 `cat`(读)与 `echo >`(写)需要 shell 解析,
  复杂度高且收益有限因为可被绕过)。
- **已知局限**:base64 编码命令、`cd` + 相对路径、运行时拼路径可绕过静态扫描。
  `_screen_command_paths` 自认"这只是降噪"。因此方向 C 是**尽力拦截**,不是绝对门——
  绝对门靠方向 B 把文件工具路径堵死 + 方向 D 收窄沙箱可写面。
- 与方向 A 的关系:方向 A 是"floor 扩展 + in-root 也查 floor"用于文件工具路径;
  方向 C 是同一个 floor 扩展被 `_screen_command_paths` 天然消费用于 shell 路径。
  两者共享一个 floor 谓词,不重复。

### 方向 D:`validate_project_dir` 拒绝绑定到 `ROOT_DIR`(治本但激进)

- `config/path.py:447-471` 增加:会话绑定目录不得 `== ROOT_DIR` 或 `is_relative_to(ROOT_DIR)`
  的源码子树。需带开发者模式开关(`SHERRY_ALLOW_SELF_PROJECT=1`)绕过——AGENTS.md 的大量
  `uv run pytest`/`uv run basedpyright agent/` 命令假定项目目录是 checkout。
- 优点:从源头消除"默认指向自身"的最大风险面;**同时收紧 shell 的沙箱可写面**——
  terminal 的 `_session_writable_roots`(`terminal.py:326`)读 session root,绑定到 ROOT_DIR
  外即沙箱不再允许写整棵 checkout。这是方向 C 尽力门之外的**真正屏障**(沙箱是 OS 级限制,
  不像 `_screen_command_paths` 可被 base64 绕过)。
- 缺点:开发者工作流被打断(需先 `SHERRY_PROJECT_DIR` 指向别的 checkout 或副本);
  **不解决"项目目录是别处、agent 仍可绝对路径写 `agent/core.py`"**(方向 B 挂点兜)。
  需与方向 B 组合。

**推荐组合:方向 B(主,挂点前移到 resolve_tool_path)+ 方向 C(floor 扩展,shell 天然查)
+ 方向 D(可选,治本+收紧沙箱可写面,带开发者模式开关)。**
方向 A 的 floor 扩展与方向 C 共享同一 floor 谓词,不单列;方向 A 的"in-root 也查 floor"
部分由方向 B 的 resolve_tool_path 挂点替代(能走 HITL 而非只能 hard-deny)。

---

## 六、待决策项

1. **回放门 formatter**——**已关闭**:由 `计划_借鉴Agensh自组织多代理机制.md` 的 P0-4
   解决(`_safe_argv` 对 `black`/`prettier`/`ruff format` 强制 `--check`,否则跳过回放)。
   `--check` 只读不写文件,不触发写白名单;可写形态在回放时根本不执行。
2. **`workspace/memory/` 读默认禁**是否影响 agent 自身功能?已确认 `context_engine` 走
   MesMemory SQLite 不读 `*.md`;`memory` 工具写 `MEMORY.md` 走独立路径不经
   `resolve_external_path`(见 §2.2「不误伤前提」),读白名单对它零影响。**已无风险,
   可关闭此项**。
3. **隔离合并**(`workspace_merge.py`)与子代理写:子代理若在隔离工作树里改了自身源码,
   合并回父树时应否拦截?**倾向:拒绝合并 + 记 conflict**(走现有 `report.conflicts`
   机制,`merge.py:194`),**不走 HITL**——`merge_isolated_workspace` 不经
   `resolve_external_path`/`resolve_tool_path`(它是 sherry 进程逻辑直接写 `parent_root`),
   且合并是自动流程(子代理完成→announce→merge),走 HITL interrupt 会卡住整个合并管线。
   在 `_apply_bytes`/`_merge_one`(`merge.py:217-270`)里加判定:路径落在 ROOT_DIR 内
   且不在写白名单 → 记 conflict、父树不动(与 CAS 冲突语义一致,不引入新阻塞点)。
4. **shadow_git 快照**对自身源码的写入:`ensure_checkpoint` 在 `.sherry/shadow-git/` 下
   存快照,这是 sherry 自己的写,不是 agent 写——应**豁免**(不纳入写白名单判定,且本就不经 file_tools)。
5. **HITL 卡片文案**:需四语言(EN/ZH/JA/KO)警示文案,走 `chatBox.*` / `hitl.*` 既有
   i18n 键。
6. **测试**:需新增 `tests/agent/middlewares/test_path_guard/` 下"自身源码写白名单"与
   "敏感配置读白名单"用例,覆盖文件工具与 shell 两条路径。

---

## 七、验收标准

- [ ] 默认配置下,`write_file` 写 `<project_dir>/.sherry/codeintel/foo` **不**触发 HITL(白名单内放行)。
- [ ] 默认配置下,`write_file` 写 `TEMP_DIR/foo`、`SESSIONS_DIR/<当前 sid>/plans/x.md` **不**触发 HITL(白名单内放行)。
- [ ] 默认配置下,`write_file` 写 `SESSIONS_DIR/<其他 sid>/x` 触发 HITL(跨会话,白名单外)。
- [ ] 默认配置下(**不设 `SHERRY_PROJECT_DIR`**,root==`ROOT_DIR`),`patch_file` 改
      `agent/core.py` 触发 HITL 卡片(白名单外)。**此场景方向 B 旧挂点
      (`resolve_external_path` 1a)不触达,必须由 `resolve_tool_path` 挂点覆盖——
      若测试环境设了 `SHERRY_PROJECT_DIR` 指向别处就变成了 1a 能覆盖的场景,会漏掉致命缺陷。**
- [ ] 默认配置下,`read_file` 读 `TEMP_DIR/foo`、`SESSIONS_DIR/<当前 sid>/plans/x.md` **不**触发 HITL(读白名单内放行)。
- [ ] 默认配置下,`read_file` 读 `SESSIONS_DIR/<当前 sid>/evicted/x` 触发 HITL(含历史对话,读白名单外)。
- [ ] 默认配置下,`read_file` 读 `agent/core.py`(自身源码)触发 HITL 卡片(读白名单外)。
- [ ] 默认配置下,`read_file` 读 `.models` 触发 HITL 卡片。
- [ ] `terminal` 执行 `cat .models` / `echo x > agent/core.py` 被拦截(尽力门,
      静态可解析的命令必须拦下;base64/动态拼路径的局限需在文档明示)。
- [ ] 用户 HITL 批准后单次放行,批准不持久化(下一次仍需审批)。
- [ ] YOLO 全局开关 / 会话 allowlist **不能**绕过这两条约束(写白名单 / 读白名单)。
- [ ] **不误伤验证**:`memory` 工具写 `workspace/memory/MEMORY.md`、`skill_manage` 写
      `AUTO_SKILLS_DIR`、`exit_plan_mode` 落盘 `sessions/<sid>/plans/`、evidence ledger /
      shadow_git / checkpointer / heartbeat 写各自路径,均正常——它们走独立工具/进程路径
      不经 `resolve_external_path`,白名单零影响。
- [ ] 四语言 HITL 文案到位。
- [ ] 回归测试覆盖文件工具 + shell 两条路径,纳入 CI gate。
- [ ] **subagent 隔离**:subagent 读写 `SESSIONS_DIR/<main sid>/` 或 `<sibling sid>/` 被拒
      (隔离黑名单,不走 HITL);subagent 写自己的隔离工作树(`SRC_DIR/data/isolated/<child>`、
      `<child_root>/.sherry/`)正常(隔离黑名单不挡自身)。
- [ ] **白名单内不需要 HITL**:`write_file` 写 `TEMP_DIR`、`read_file` 读 `agent/core.py`
      (源码)均直接放行,无 HITL 卡片——不打断 main agent 正常功能。
