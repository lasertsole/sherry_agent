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
| 记忆 / 技能文件注入 | **记忆写入扫描**：三条写入路径全部拦截（用一套面向散文的模式表，因为一条*介绍系统*的笔记不是攻击），外加安装时的技能扫描门禁 | — |
| 密钥泄漏到日志 / 工具输出 | **脱敏引擎**：日志管线（始终开启，含异常文本）与易泄漏的工具输出；子进程用 `scrub_env` | 只能去掉它认得出的那些形状，仅此而已 |
| URL 凭证泄漏 | **URL 脱敏**：更多协议、嵌套百分号解码（深度 8）、查询参数与预签名参数 | — |
| 路径遍历 | `PathGuard` + `O_NOFOLLOW` | — |
| Shell 注入 | `agent/middlewares/humanInTheLoop/detection.py` 清单（12 条 hardline + 59 条 dangerous） | — |
| 沙箱逃逸 | `bwrap` / `seatbelt` | — |
| 推理块泄漏到流式输出 | **内联 CoT 重定向**：思考内容被搬到推理通道，而不是被丢弃 | 完全不输出标签的模型按定义不受影响 |
| 终端输出中的控制序列 | **控制序列剥离**：`terminal` 两个 spawn 落点都施加 | — |
| 日志里的渠道标识（用户 / 聊天） | **稳定假名**：渠道日志边界处替换 | 路由表与回复目标按设计保留原值 |
| 子进程 env hijack（`LD_PRELOAD`、`LD_LIBRARY_PATH`、`BASH_ENV` 等） | **hijack 变量阻止**：启动钩子（`PYTHONPATH`、`BASH_ENV`、`ENV` 等）始终剔除；加载器变量（`LD_PRELOAD`、`LD_LIBRARY_PATH`、`DYLD_INSERT_LIBRARIES`）仅在 `SHERRY_STRICT_ENV_HIJACK=1` 下剔除——因为容器运行时会真的设置它们 | 加载器变量默认不阻止，理由见运行手册一节 |
| 跨站请求伪造（CSRF） | **Origin 允许清单** + **CSRF 守卫**：写方法（POST/PUT/PATCH/DELETE）遇 `Sec-Fetch-Site: cross-site` 一律拒绝；`Origin`/`Referer` 存在但不是 loopback 也不在允许清单时同样拒绝 | 脚本客户端三个头都不发（curl、测试客户端）——它靠 token 鉴权，不靠这道守卫 |
| 服务端请求伪造（SSRF） | **SSRF 判定**覆盖每个被抓取的 URL：非全局目标（私有、loopback、云元数据、RFC 2544、IPv6 ULA）拒绝；连接**钉在已校验的地址**上；每一次重定向逐跳复查 | fake-ip 代理主机会把公网域名解析到被拒网段——用文档化的开关作为逃生口 |
| 渲染内容中的 XSS | 客户端 DOMPurify 允许清单 + `vue/no-v-html` + **服务端安全响应头**（`script-src 'self'`、`object-src 'none'`、`nosniff`、防嵌套） | CSP 是第二层：客户端净化器与载荷同上下文运行 |
| 借 `terminal` 手工发起 SSRF（模型被说服执行 `curl`） | HITL 审批模式：云元数据端点、以及**带写入**的 loopback 请求 | 模式是启发式而非边界：命令可以换个说法绕开 |
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
* **异常也在覆盖范围内**。loguru 在写出时才从活动栈帧渲染 traceback，patcher 看不到那段文本；因此 patcher 自己渲染它（用 loguru 自己的 formatter，所以 `> File …` 版式仍在）、脱敏它，并清空记录的异常字段，避免任何 sink 再打印一次。这关掉了密钥从失败调用里出走的两个口子：异常自身的消息，以及 `diagnose` 的栈帧变量转储。渲染时**刻意不带**变量值——变量转储会原样打印栈帧局部变量，而聊天 ID 或一段引用文本没有任何规则认得出。

## 写入与输出边界

有四个面改写的是**离开进程**的东西，而不是进入进程的东西。每一个都是因为原始形态对下游无用或有害才加的，而且每个都是卫生措施，不是边界。

| 面 | 模块 | 做什么 |
|---|---|---|
| 记忆写入（`memory` 工具的 `add`/`replace` 与 flush 路径） | `agent/tools/memory.py` | 用一套面向散文的模式表扫描后拒绝写入；被拦下的条目从不落盘。它不复用上面的扫描器分档，因为记忆条目是*介绍系统*的散文——一条提到 `.bashrc` 或 `KEY=` 变量的笔记是笔记，不是攻击 |
| 捕获到的终端输出 | `agent/security/terminal_output.py` | 去掉 CSI/OSC 与其余转义序列、C0 控制字节，并施加回车覆写语义（`10%\r100%` → `100%`；`\r\n` 行尾保留其文本）。`terminal` 的两个 spawn 落点（同步与异步）都施加 |
| 携带内联推理的回答文本 | `agent/security/think_scrub.py` | 把 `<think>`/`<thinking>`/`<reasoning>` 的内容从回答通道搬到客户端渲染思考块的推理通道。每回合一个 scrubber，且与切分无关：分块边界由 provider 决定，所以标签在任意位置被切开都必须与整段一致 |
| 渠道用户 / 聊天标识 | `agent/security/pii.py` | 日志写 `«pii:<12 位十六进制>»` 而不是平台标识。跨进程稳定，因此"收到"与"已发送"在之后的日志文件里仍可关联；原值保留在它发挥作用的地方（路由表、回复目标、平台 SDK 调用） |

## 网络边界

进程与网络之间有三道闸门，各自守在威胁真正可达的那一层：

| 边界 | 机制 | 说明 |
|---|---|---|
| 入站写请求 | `server/trigger/csrf.py`——写方法（`POST`/`PUT`/`PATCH`/`DELETE`）遇 `Sec-Fetch-Site: cross-site` 拒绝；`Origin`/`Referer` 存在但既非 loopback 也不在允许清单时拒绝 | 浏览器设置的头是网页伪造不了的信号；`server/trigger/auth.py` 的 Origin 门仍是第一层；三个头都不发的客户端（curl、测试客户端）放行——认证由 token 承担 |
| 入站响应 | `server/trigger/security_headers.py`——每个响应（含被拒的响应）都带 `Content-Security-Policy`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy` | `script-src 'self'` 与 `object-src 'none'` 是客户端净化器被绕过之后仍然生效的那层；`GATEWAY["csp"]` 可覆盖策略，字面值 `disabled` 则不输出该头 |
| 出站抓取 | `pub/func/validator/public_url.py`（判定）+ `pub/func/validator/safe_fetch.py`（传输） | 只要解析出的地址**不是全部**全局可达就在开 socket 之前拒绝；随后 socket 直接连到已校验的那个地址（主机名照旧走 `Host`/SNI），因此第二次 DNS 应答无法把连接引向别处；每次重定向逐跳重复这一检查 |

shell 是网络守卫看不见的第四条路径：HITL 审批清单（`agent/middlewares/humanInTheLoop/detection.py`）带了云元数据端点（那种"读了凭据再由模型复述回来"的形状）与**带写入**的 loopback 请求两类模式。而普通的 `curl http://127.0.0.1/…` 读取刻意不拦——为每次本地探测弹审批会训练操作员无脑点过。

## 安全策略

**唯一硬边界是操作系统。** 进程隔离、文件权限、沙箱后端与网关鉴权边界，才是攻击者必须真正攻破的东西。代理在**进程内**做的一切都是启发式：

| 层 | 它是什么 | 为何不是边界 |
|---|---|---|
| Prompt 注入扫描器、不可信输出围栏 | 检测与标注 | 模型可能被说服越过围栏；模式也可能被绕开表述 |
| 密钥脱敏、标识假名化、控制序列剥离 | 在输出边界做卫生性改写 | 只能去掉规则认得出的东西，而且原值通常仍存在于上游 |
| 路径防护、HITL 允许清单、危险命令清单 | 对已知危险形状的拒绝规则 | 拒绝规则天生不完整 |
| env 剥离、lane 上限、迭代预算 | 缩小爆炸半径 | 它们假定子进程本身不是攻击者的代码路径 |

我们接受并明确写出的运行结论：

* 模型输出对任何消费方都是**不可信输入**——不要让代理的话直接驱动特权动作，除非有 OS 级校验；
* 代理能读到的东西最终都可能泄漏：只给它任务需要的凭证；
* `terminal` 与 `python_repl` 以操作员的 OS 身份运行（仅去掉密钥名的环境变量），所以真正的上限是**该账号的操作系统权限**，不是沙箱*配置*；
* 无视围栏的顽固模型、以及持有同一账号的恶意操作员，都不在防御范围内：我们防御的是**从别处来的内容**，不是运行代理的人。

## 运行手册

**启动检查。** 128K 上下文下限会让进程拒绝启动（两个 LLM 都查）；网关每次启动重新签发 token，因此跨重启仍打开的页面必须重载后才能重连；密钥只放 `.env`（仅环境变量——配置文件不是密钥仓库）。

**该盯什么。** `logs/output/error/` 记录失败；与安全相关的行有：

| 日志信号 | 含义 |
|---|---|
| `refusing WebSocket handshake` | 客户端 token 过期或缺失——重启后属预期 |
| 消息里出现 `«redacted»` | 有凭证形状的字符串进了日志记录并被掩码 |
| 消息里出现 `«pii:…»` | 某个渠道用户/聊天标识被假名化（同一字符串就是同一个会话） |
| `csrf guard: refused` | 一个写请求带着跨站或外来来源到达——敌对页面，或丢了 Origin 的客户端 |
| `refused by the SSRF guard` | 某个 URL 解析到非全局地址，未被抓取 |
| `Potential security threat detected: <id>` | 注入扫描器在工具输出上命中 |
| 沙箱 / 拒绝相关行 | 某次工具调用被拒绝规则拦下 |

**怀疑泄漏时。** 先轮换凭证（环境配置 / `.env`），再按前缀搜日志——脱敏哨兵能告诉你"这种形状的值确实进过日志"，而 MesMemory 保存着它来自哪段工具输出。没有命中任何模式的泄漏，恰恰是启发式无法排除的部分：对解释不清的东西按最坏情况处理。

**加固开关。** `SHERRY_STRICT_ENV_HIJACK=1` 把子进程环境阻止扩展到加载器变量——对自有加载器环境的主机是对的，对会设置它们的容器运行时是错的（本仓库的开发机就是后者）。`UNTRUSTED_OUTPUT["enabled"]` 与 `REDACTION["tool_output_enabled"]` 控制模型路径上的保护；`SHERRY_REDACT` 在导入时快照，因此会话无法通过写任何它能写的东西来关掉脱敏。

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

