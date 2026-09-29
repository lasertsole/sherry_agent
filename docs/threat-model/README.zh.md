# 威胁模型

Sherry 防御什么、信任边界在哪里，以及同样重要的一点——哪些防护是操作系统强制的边界、哪些只是进程内的启发式。这里是总图；操作系统隔离的细节由[沙箱文档](../sandbox/README.zh.md)负责。

## 信任边界

| # | 边界 | 跨越点 | 由谁强制 |
|---|---|---|---|
| 1 | 用户 → Agent | WebSocket 回合 | 网关鉴权（Origin 允许列表 + 每次启动的 token） |
| 2 | LLM → 工具 | 工具调用 | HITL 审批门、`agent/middlewares/humanInTheLoop/detection.py` 危险命令清单 |
| 3 | 工具输出 → 模型上下文 | 工具结果 | **不可信输出围栏**、Prompt 注入扫描器、工具结果驱逐 |
| 4 | 子 Agent → 父 Agent | announce 管道 | 完成门禁、`SubagentCompletionDrain` |
| 5 | MCP 服务器 → Agent 进程 | 工具结果 | 不可信输出围栏（`mcp_` 前缀规则；目前没有 MCP 工具） |
| 6 | HTTP 客户端 → 网关 | HTTP 路由 | 网关鉴权中间件、查询参数类型化转换 |
| 7 | 文件系统 → Agent | 文件工具 | `PathGuard`、`O_NOFOLLOW`、虚拟路径解析 |
| 8 | 沙箱子进程 → 宿主进程 | 子进程派生 | `scrub_env`、`bwrap`/`seatbelt` 隔离 |
| 9 | 记忆 / 技能文件 → 系统提示词 | 提示词组装 | 安装时的技能扫描门禁；**写入时注入拦截待实现** |
| 10 | TaskFlow step 结果 → 下游 step | DAG 边 | 期望→实际闭环：schema 门、step judge、证据台账 |

## 数据分类

| 类别 | 示例 | 存放位置 |
|---|---|---|
| 敏感 | API key、token | 环境变量；任何子进程之前由 `scrub_env`（`agent/tools/pub_base/env_scrub.py`）剥离 |
| 私密 | 对话历史 | MesMemory SQLite（WAL） |
| 内部 | 工具结果 | 消息列表 + 驱逐文件 |
| 不可信 | 网页内容、终端输出、MCP 结果 | 工具消息——是**数据**，永远不是要执行的指令 |

## 威胁分析

| 威胁 | 现有防护 | 差距 |
|---|---|---|
| 间接 prompt 注入（工具输出） | **不可信输出围栏**（伪造分隔符已失效）、工具结果驱逐、不可见 Unicode 扫描 | — |
| 网页/终端文本中的注入指令 | Prompt 注入扫描器（见下） | 需要各调用面自行应用 |
| 记忆 / 技能文件注入 | 安装时的技能扫描门禁 | **记忆写入时的阻断扫描** |
| 密钥泄漏到日志 / 工具输出 | **脱敏引擎**：日志管线（始终开启）与易泄漏的工具输出；子进程用 `scrub_env` | `diagnose=True` 下的 traceback 局部变量未覆盖（见下） |
| URL 凭证泄漏 | **URL 脱敏**：更多协议、嵌套百分号解码（深度 8）、查询参数与预签名参数 | — |
| 路径遍历 | `PathGuard` + `O_NOFOLLOW` | — |
| Shell 注入 | `agent/middlewares/humanInTheLoop/detection.py` 清单（12 条 hardline + 59 条 dangerous） | — |
| 沙箱逃逸 | `bwrap` / `seatbelt` | — |
| 推理块泄漏到流式输出 | — | **think scrubber** |
| 子进程 env hijack（`LD_PRELOAD`、`BASH_ENV` 等） | 按名称剥离密钥变量 | **hijack 变量阻止** |
| 上传端点内容伪造 | 网关鉴权（Origin + token） | **字节签名与声明类型一致性校验** |

## 不可信输出围栏

`agent/security/untrusted_wrapper.py` 实现，由
`agent/middlewares/context_eviction/core.py` 对每个工具结果应用。来自攻击者可控工具的
结果会被包进 `<untrusted_tool_result source="…" id="…">` 块，块内的提示语声明这些内容是**数据**而非指令。

| 性质 | 做法 |
|---|---|
| 适用范围 | `web_search`、`tavily_search`（配好 API key 后同一个工具以 Tavily 自己的名字出厂）、`message_search`，以及任何 `mcp_` 工具 |
| 分隔符防伪 | 载荷里的闭合标签在包装前被改写成 `</untrusted-tool-result>`，无法提前结束该块 |
| 顺序 | 先 evict 再包装，因此围栏包住的正是模型实际看到的那份预览 |
| 原文 | 只有模型视图被围栏；内层持久化持有的消息对象从不被修改 |
| 开关 | `config/features/agent_side/untrusted_output.py` 里的 `UNTRUSTED_OUTPUT["enabled"]`，提示语也在同一处 |

它是**围栏，不是边界**：模型仍可能被说服无视它。价值在于把边界写在模型阅读的位置，并让最廉价的伪造（提前闭合该块）失效。

## 密钥脱敏

`agent/security/redact.py` 负责在文本里掩码凭证，落点有两处，开关刻意不同：

| 面 | 由谁施加 | 开关 |
|---|---|---|
| 每一条日志记录（控制台 + 三个轮转文件） | `agent/security/redact_formatter.py`（由 `logs/logger.py` 装载的 loguru patcher） | 始终开启——日志文件比会话活得久，读它的人从没见过那个密钥 |
| 天生易泄漏的工具输出（`terminal`、`python_repl`、不可信工具集、`mcp_*`） | 与围栏同一个中间件 | `config/features/agent_side/redaction.py` 里的 `REDACTION["tool_output_enabled"]` |
| 文件工具（`read_file`、`patch_file`、`write_file`） | — | 刻意不脱敏：agent 要编辑自己的配置，掩码会让"读回再写回"变成有损操作 |

覆盖家族：vendor 密钥前缀（`sk-`、`ghp_`、`AKIA`、`xox*`、`AIza`、`hf_` 等）、各种配置形状里的密钥名赋值（`.env`、INI、YAML、TOML、JSON——含 `"CURATOR_API_KEY"` 这类带前缀键与引号内含空格的值）、`Authorization` / `X-API-Key` 头、JWT、PEM 私钥块、URL 凭证与查询参数。

可以依赖的性质：

* **幂等**——哨兵不匹配任何家族，重复脱敏不改变结果。
* **开关在导入时快照**（`SHERRY_REDACT`）：模型就算说服某处写下 `export REDACT=false` 也无法在会话中途解除；日志路径完全不受该开关影响。
* **宁可过度脱敏**。要求值"看起来像凭证"会让全小写密码漏网，因此代码里把密钥名变量自我赋值的写法也会被改写（例子见该规则自身的注释）；语料测试钉住"改写会真正造成伤害"的面（文档）。
* **已知边界**：异常的 traceback 由活动栈帧渲染，因此只作为失败栈帧局部变量存在过的密钥，仍可能出现在 error sink 的 `diagnose` 转储里。

## Prompt 注入扫描器

`agent/security/threat_patterns.py` 扫描攻击者可控的文本并返回命中 ID。三个层级，后者是前者的超集：

| Scope | 增加的模式 | 适用场景 |
|---|---|---|
| `all` | 经典注入（ignore/disregard instructions、角色劫持、系统提示词套取）、密钥外带、隐藏 HTML 注释 | 每个工具结果 |
| `context` | C2 / promptware 形态（节点注册、心跳、拉取 tasking、已知框架名）、改写指令文件 | 工具结果与上下文文件 |
| `strict` | SSH 后门、shell rc 持久化、硬编码的 provider 密钥、不可见 Unicode | 记忆写入、技能安装 |

```python
from agent.security.threat_patterns import scan_for_threats, first_threat_message

if findings := scan_for_threats(page_text, scope="context"):
    block_reason = first_threat_message(page_text, scope="context")
```

调用方依赖的性质：

* **永不抛异常、永不修改**传入内容；未知 scope 回落到 `all`——拼错一个名字不该让工具挂掉。
* **扫描有界**：只看前 65,536 个字符，且每条模式的每个量词都有界——构造输入无法把扫描本身变成拒绝服务。
* **消息不回显命中的原文**：`first_threat_message` 只给命中 ID，因此命中结果可以安全地写日志或展示，不会二次注入。
* 它是**启发式，不是边界**。唯一硬边界是操作系统隔离（见[沙箱文档](../sandbox/README.zh.md)）；扫描器的作用是抬高注入成本，并给调用方一个可据以行动的信号。

以本仓库自身产物实测：112 个真实日志/工具输出文件、3.0 MB 文本，扫描耗时 0.50 s（约 6 MB/s），**零命中**；把同一语料加一段注入载荷后立刻报出——说明这些层级在真实内容上安静，同时仍能抓住它们存在的意义。

