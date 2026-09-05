# 🍊 EMA AI Agent - 橘雪莉

![Python](https://img.shields.io/badge/Python-3.13-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.3+-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**English**](README.md) · [**中文**](README.zh.md) · [**한국어**](README.ko.md) · [**日本語**](README.ja.md)

> **一个基于 LangChain/LangGraph 与多模态技术的深度角色扮演 AI Agent。**

## ✨ 简介

EMA AI Agent 是一个高度拟人化、具备长期记忆与复杂推理能力的 AI Agent 系统。它不仅仅是一个聊天机器人，更是一个拥有独立 **Persona（人格）**、动态 **技能系统**，并可通过定时任务与后台子代理主动行动的虚拟伙伴。

Agent 的角色 **橘雪莉（Sherry）** 是一位自封的少女侦探：外表永远开朗活泼，内心却冷静缜密。整个系统旨在支持沉浸式、可持久化的角色扮演，让记忆跨会话不断积累。

---

## 🚀 核心特性

### 1. 🧠 分层记忆系统（Context Engine）
- **短期会话记忆**（[MesMemory](context_engine/README.md)）：每条 human/ai/tool 消息持久化到 SQLite（WAL 模式），并自动建立 FTS5 索引——包含面向中文全文检索的 trigram 分词表
- **历史检索**：支持最近 N 轮、分页历史、指定轮次范围查询，并格式化为提示词上下文
- **会话检查点**：线程安全的异步 SQLite checkpointer（`langgraph-checkpoint-sqlite`）跨重启持久化 Agent 状态，过期检查点自动清理
- **对话摘要**：Summarization 中间件在对话中途用 auxiliary LLM 压缩过长历史
- **私有知识图谱 RAG**：`multimodal_rag` 技能将文档/文件夹索引为实体关系图（内置 vendored LightRAG + RAG-Anything，基于 `snkv` 向量存储），并通过多跳图检索回答问题
- ▶️ _详见 [Context Engine README](context_engine/README.md) 了解架构、数据模型与 API_

### 2. 🛠️ 动态技能系统
- **SKILL.md 标准**：技能是带 YAML frontmatter 的 Markdown 文件（`name`、`description`、可选 `scope: all | main_only | subagent_only`）——loader 会自动发现 `skills/` 下的所有 `SKILL.md`
- **内置技能**（[skills/builtin/](skills/builtin/)）：`cron`、`heartbeat`、`clawhub`（GitHub 技能安装器）、`skill_creator`（自动生成新技能）、`image_to_text`、`speech_to_text`、`video_text_to_text`、`text_to_image`、`multimodal_rag`、`code_wiki`、`llm_wiki`
- **技能管理工具**：Agent 可在运行时列出、查看、管理技能；第三方上传的技能（`skills/plugins/`）默认停用，需显式启用
- **SkillSpector 安全扫描**（[server/service/skill_scanner.py](server/service/skill_scanner.py)）：第三方技能在启用前由 NVIDIA SkillSpector 扫描（静态 YARA/规则分析 + 可选的 LLM 语义分析，使用 auxiliary LLM）；被标记的技能将被禁止安装
- **技能 Curator**：context engine 的 curator 线程维护 `skills/auto/` 下的自动学习技能
- **工具超时**：工具调用受 `TOOL_CALL_TIMEOUT_MINUTES`（默认 5）限制，防止死锁
- ▶️ _详见 [Middlewares README](agent/middlewares/README.md) 了解中间件流水线（护栏、迭代预算、HITL、规范化、摘要、多模态处理）_

### 3. 🤖 多层级子代理系统
- **7 个运行时工具**：`sessions_spawn`、`sessions_yield`、`sessions_send`、`sessions_kill`、`sessions_steer`、`agents_list`、`subagents_list`
- **层级角色**：深度受限的嵌套（默认最大 2 层，硬上限 2），MAIN → ORCHESTRATOR → LEAF 角色与最小权限工具作用域
- **上下文模式**：ISOLATED（全新上下文）或 FORK（复制父级对话记录），支持文件附件
- **可靠投递**：结果通过 EventBus announce 流水线回传，具备幂等校验与指数退避重试
- **持久化注册表**：运行记录持久化到 SQLite；sweeper 负责恢复孤儿任务，followup 检查器在配置了运行超时时强制超时（默认不配置）
- **Swarm 模式**：批量子任务执行，FIFO 调度与并发数控制
- ▶️ _详见 [Subagent System README](agent/tools/subagent/README.md) 了解完整架构_

### 4. 🌐 多渠道接入
- **Robyn 后端**（[server/](server/)）：异步 HTTP API + WebSocket（`/sessions/ws`），监听 `127.0.0.1:8080`，并通过 `/static`、`/images`、`/audio`、`/video` 提供上传媒体文件
- **桌面客户端**（[client/](client/)）：Tauri 2 + Nuxt 4（Vue 3 + TypeScript）SPA，支持系统托盘、全局快捷键、离线历史缓存（Dexie/IndexedDB）、明暗主题与国际化
- **QQ 机器人**：通过插件系统接入 QQ 频道适配器（[plugins/channels/qq/](plugins/channels/qq/)）
- **消息总线**（[bus/core.py](bus/core.py)）：内部异步队列解耦渠道与 Agent 核心

### 5. 👁️ 多模态交互
- **图像理解（ITTT）**：Image-to-Text 视觉模型，识别与分析用户上传的图片
- **视频理解（VTTT）**：Video-Text-to-Text 模型，分析视频内容
- **语音识别（STT）**：基于 FunASR 的本地语音转文字
- **文生图（TTI）**：通过 `text_to_image` 技能根据文字描述生成图片
- **文档解析**：基于 MinerU 的多模态文档摄取，服务于知识图谱 RAG 流水线

### 6. ⏰ 定时与主动行为
- **Cron 服务**（[skills/builtin/core/cron/](skills/builtin/core/cron/scripts/README.md)）：支持一次性（`at`）、间隔（`every`）与 cron 表达式（`cron`，基于 croniter + 时区）三类定时任务，持久化到 JSON 任务文件，带运行历史与渠道投递
- **Heartbeat 服务**（[skills/builtin/core/heartbeat/](skills/builtin/core/heartbeat/README.md)）：周期性唤醒（默认 30 分钟），检查 `HEARTBEAT.md` 中的待办任务，由 LLM 决定 skip/run，结果经过通知门控过滤

---

## 🏗️ 技术栈

基于 **Python 3.13**（依赖管理使用 [uv](https://docs.astral.sh/uv/)），核心技术如下：

| 模块 | 技术 |
| :----- | :--------- |
| **Agent 框架** | LangChain 1.3+（`create_agent` + 中间件）、LangGraph 编译图 |
| **检查点** | langgraph-checkpoint-sqlite（线程安全异步 SQLite saver） |
| **Web 服务器** | Robyn（HTTP + WebSocket + 静态托管） |
| **数据库** | SQLite（aiosqlite，FTS5 全文检索，WAL 模式） |
| **图谱 RAG** | 内置 vendored LightRAG + RAG-Anything（multimodal_rag 技能）、`snkv[vector]` 存储 |
| **本地推理** | llama-cpp-python（GGUF：bge-m3 embedding、bge-reranker-v2-m3 reranker、auxiliary/ITTT/VTTT 模型）、FunASR（STT） |
| **文档解析** | mineru-vl-utils |
| **联网搜索** | langchain-tavily（Tavily API） |
| **LLM 提供商** | langchain-openai、langchain-deepseek、langchain-community + 20+ 提供商注册表（OpenAI、Anthropic、DeepSeek、智谱 GLM、DashScope 通义、Gemini、Moonshot Kimi、MiniMax、Groq、OpenRouter、SiliconFlow、火山引擎、Azure OpenAI、Ollama、vLLM 等） |
| **结构化输出** | instructor、json_repair |
| **MCP** | langchain-mcp-adapters（在 `plugins/mcp_server/` 中配置服务器） |
| **任务调度** | croniter、asyncio |
| **异步消息** | asyncio 队列（MessageBus、EventBus） |
| **媒体处理** | OpenCV（headless）、Pillow、websockets / websocket-client |
| **桌面客户端** | Tauri 2 + Nuxt 4（Vue 3、TypeScript、pnpm） |
| **日志** | loguru（可选 LangSmith 追踪） |

---

## 📂 项目结构

```text
EMA_AI_agent/
├── agent/                  # Agent 核心逻辑
│   ├── core.py             # 主 Agent 循环（LangChain create_agent → LangGraph 图）
│   ├── smart_tool_node.py  # 工具节点补丁（幂等工具并行执行）
│   ├── stream_repetition_guard_wrapper.py # 流式输出重复防护
│   ├── checkpointer/       # 线程安全异步 SQLite checkpointer
│   ├── middlewares/        # 中间件流水线（摘要、护栏、HITL 等）
│   └── tools/              # Agent 可用工具
│       ├── subagent/       # 多层级子代理系统（spawn/registry/swarm 等）
│       ├── file_tools/     # 文件 I/O 工具（读、写、补丁、搜索）
│       ├── skill_tools/    # 技能管理工具（列表、查看、管理）
│       ├── pub_base/       # 共享工具基础组件
│       ├── mcp_plugin.py   # MCP 工具集成
│       ├── web_search.py   # 联网搜索工具（Tavily）
│       ├── python_repl.py  # Python 代码执行
│       ├── terminal.py     # 终端命令执行
│       ├── memory.py       # 记忆查看工具
│       └── message_search.py # 会话 FTS5 搜索工具
│
├── bus/                    # 消息总线（异步队列）
│   └── core.py             # MessageBus —— 入站/出站队列
│
├── channels/               # 渠道接口定义
│   ├── base.py             # 渠道抽象基类
│   ├── manager.py          # 渠道生命周期管理器
│   └── registry.py         # 渠道注册
│
├── client/                 # 桌面客户端（Tauri 2 + Nuxt 4，pnpm）
│   ├── app/                # Nuxt 4 SPA 源码（Vue 3）
│   ├── src-tauri/          # Tauri 2 原生壳（Rust）
│   └── README.md           # 客户端文档
│
├── config/                 # 集中配置
│   ├── __init__.py         # API 主机/端口（127.0.0.1:8080）
│   ├── path.py             # 文件路径配置
│   ├── schema.py           # 配置模型
│   └── num.py              # 数值/调优参数
│
├── context_engine/         # 记忆引擎（MesMemory）
│   ├── core.py             # 历史检索与 FTS5 搜索 API
│   ├── store/              # 会话消息存储（SQLite + FTS5，WAL）
│   └── curator/            # 自动技能维护
│
├── logs/                   # 日志系统
│   ├── logger.py           # 日志配置（loguru）
│   └── output/             # 日志输出目录
│
├── models/                 # 模型封装与权重
│   ├── LLMs/               # LLM 配置（main_llm.py、reasoner_llm.py、auxiliary_llm/、reasoning_* 各提供商适配）
│   ├── ITTT_model/         # 图生文模型（云端 API 或本地 GGUF）
│   ├── VTTT_model/         # 视频理解模型（云端 API 或本地 GGUF）
│   ├── STT_model/          # 语音识别模型（FunASR）
│   ├── embed_model/        # 向量嵌入模型（本地 bge-m3 GGUF 或云端 API）
│   ├── reranker_model/     # 重排序模型（本地 GGUF 或云端 API）
│   └── extract_model/      # 实体抽取模型（第三方权重）
│   └── providers/          # LLM 提供商规范与注册表
│       └── registry.py    # 20+ 提供商的 ProviderSpec
│
├── plugins/                # 插件系统
│   ├── channels/           # 渠道插件（QQ 机器人适配器）
│   └── mcp_server/         # MCP 服务器配置
│
├── pub_func/               # 通用工具函数
│   ├── format/             # 文本格式化工具
│   ├── media/              # 媒体处理工具
│   ├── message/            # 消息处理工具
│   └── validator/          # 输入校验工具
│
├── runtime/                # 运行时状态与工具
│   ├── core.py             # 单例 Register 基类 + 按会话清理
│   ├── relation_register.py # 会话/socket 关系注册表
│   ├── state_register.py   # 状态注册表
│   ├── count_call_register.py # 用量/统计计数器
│   ├── timer_call_register.py # 定时器注册表
│   └── _callback_executor.py # 异步回调执行器
│
├── server/                 # Robyn 后端服务
│   ├── __main__.py         # 服务入口（python -m server）
│   ├── DAO/                # 数据访问对象
│   ├── service/            # 业务逻辑服务（含 skill_scanner.py）
│   └── trigger/            # 路由与处理器注册
│       ├── http/           # HTTP 端点触发器
│       ├── ws/             # WebSocket 触发器
│       ├── channels/       # 渠道入站触发器
│       └── subagent/       # 子代理结果触发器
│
├── skills/                 # 技能库（SKILL.md 定义文件）
│   ├── loader.py           # 技能自动发现与注册
│   ├── skills_snapshot.py  # 构建技能提示词快照
│   ├── auto/               # 自动学习技能（curator 维护）
│   ├── plugins/            # 第三方上传技能（默认停用）
│   └── builtin/            # 内置技能
│       ├── core/           # cron、heartbeat、clawhub、skill_creator、image_to_text、
│       │                   # speech_to_text、video_text_to_text、multimodal_rag
│       ├── text_to_image/  # 文生图技能
│       ├── code_wiki/      # 代码库 wiki 生成技能
│       └── llm_wiki/       # Markdown 知识库技能
│
├── src/                    # 运行时数据目录
│   ├── checkpoints/        # 会话检查点
│   ├── data/               # 数据存储
│   ├── store/              # 数据存储
│   ├── rag/                # RAG 索引输出
│   └── images/ audio/ video/ # 上传媒体文件（静态托管）
│
├── temp/                   # 临时文件
│
├── tests/                  # 测试套件（pytest）
│
├── type/                   # 共享数据模型
│   ├── message.py          # MultiModalMessage、Chat 等
│   ├── bus.py              # 消息总线数据模型
│   └── client.py           # 客户端数据模型
│
├── workspace/              # 角色档案与行为定义
│   ├── IDENTITY.md         # 姓名、年龄、兴趣、人际关系
│   ├── SOUL.md             # 性格反差、语言风格
│   ├── AGENTS.md           # 工具使用优先级、安全边界
│   ├── USER.md             # 用户偏好与已知信息
│   ├── HEARTBEAT.md        # Heartbeat 服务的待办任务
│   ├── character.json      # 角色配置（JSON）
│   ├── prompt_builder.py   # 档案到系统提示词的构建器
│   ├── file_sync.py        # 工作区模板懒同步（按语言）
│   ├── template/           # 人设模板（en / zh / ja / ko）
│   └── memory/             # 长期记忆存储
│
├── .env.example            # 环境变量模板
├── pyproject.toml          # Python 依赖（uv 管理）
├── uv.lock                 # uv 锁文件
├── start.sh                # 后端启动脚本
└── cron_jobs.json          # Cron 任务计划数据
```

---

## 📚 子模块文档

各主要子系统均有详细 README：

| 子模块 | 说明 | 文档 |
|-----------|-------------|---------------|
| **Context Engine** | 短期会话消息记忆（MesMemory） | [EN](context_engine/README.md) · [ZH](context_engine/README.zh.md) |
| **子代理系统** | 多层级子代理派生、并行执行与结果投递 | [EN](agent/tools/subagent/README.md) · [ZH](agent/tools/subagent/README.zh.md) |
| **中间件** | Agent 生命周期中间件流水线 | [EN](agent/middlewares/README.md) · [ZH](agent/middlewares/README.zh.md) |
| **渠道** | 渠道接口与适配器系统 | [EN](channels/README.md) · [ZH](channels/README.zh.md) |
| **桌面客户端** | Tauri 2 + Nuxt 4 桌面/移动 SPA 客户端 | [EN](client/README.md) · [ZH](client/README.zh.md) |
| **Cron 服务** | 定时/周期性 Agent 任务执行 | [EN](skills/builtin/core/cron/scripts/README.md) · [ZH](skills/builtin/core/cron/scripts/README.zh.md) |
| **Heartbeat 服务** | 周期性唤醒任务检查 | [EN](skills/builtin/core/heartbeat/README.md) · [ZH](skills/builtin/core/heartbeat/README.zh.md) |

## ⚡ 快速开始

### 1. 前置条件
- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)** —— 依赖管理器。`.venv` 由 uv 自动创建和管理，无需手动创建虚拟环境。

```bash
git clone <your-repo-url>
cd EMA_AI_agent
uv sync   # 创建 .venv 并按 uv.lock 精确安装依赖
```

### 2. 配置环境变量
复制 `.env` 模板，至少填写主聊天模型与 Tavily Key：

```bash
cp .env.example .env
```

| 变量 | 必填 | 说明 |
| :------- | :------- | :---------- |
| `MAIN_LLM_PROVIDER` / `MAIN_LLM_NAME` / `MAIN_LLM_API_BASE` / `MAIN_LLM_API_KEY` / `MAIN_LLM_MAX_TOKEN` | ✅ | 主聊天模型（需支持 JSON 输出与工具调用） |
| `MAIN_LLM_ENABLE_THINKING` / `MAIN_LLM_REASONING_EFFORT` | — | 通用推理开关，按提供商映射（DeepSeek / OpenAI / GLM / Anthropic） |
| `TAVILY_API_KEY` | 使用联网搜索时必填 | 启用联网搜索工具 |
| `REASONER_LLM_*` | — | 思维链推理模型 |
| `AUXILIARY_LLM_*` | — | 轻量模型，用于摘要/简单任务（模板默认云端 API；设 `AUXILIARY_LLM_MODEL_LOCAL=true` 使用本地 GGUF 模型） |
| `ITTT_*` / `VTTT_*` / `TTI_*` / `STT_*` | — | 图像 / 视频 / 文生图 / 语音模型配置 |
| `RERANKER_*` / `EMBEDDING_*` | — | 检索所需的重排序与嵌入模型（见下方模型说明） |
| `SKILL_SCANNER_ENABLED` / `SKILL_SCANNER_LLM` | — | SkillSpector 安全扫描开关（默认开启） |
| `TOOL_CALL_TIMEOUT_MINUTES` / `LOG_LEVEL` | — | 工具超时（5 分钟）与日志级别（INFO） |
| `WORKSPACE_TEMPLATE_LANG` | — | 人设模板语言：`en` / `zh` / `ja` / `ko`（首次使用时懒拷贝） |
| `LANGSMITH_*` | — | 可选的 LangSmith 追踪 |

### 3. 模型说明（HuggingFace 自动下载）
配置为**本地 GGUF** 模式的模型会在首次使用时自动从 Hugging Face 下载到 `models/<model>/model_weight/`，无需手动下载：

- **嵌入模型**：`EMBEDDING_MODEL_LOCAL=true`（默认）使用本地 `bge-m3` Q8_0 GGUF，首次运行时自动下载。
- **重排序模型**：`.env` 模板默认使用**云端 API**（`RERANKER_MODEL_LOCAL=false`，OpenAI 兼容的 `bge-reranker-v2-m3`）。设为 `true` 可切换为本地 GGUF 重排序模型（约 636 MB，自动下载）。
- **ITTT / VTTT / Auxiliary LLM**：模板默认云端 API；设 `*_MODEL_LOCAL=true` 切换为本地 GGUF 模型（同样自动下载）。

> 首次下载需要访问 huggingface.co（中国大陆用户可能需要代理或镜像）。下载中断后下次启动会继续；删除 `models/<model>/model_weight/` 可强制重新下载。

### 4. 启动后端
`start.sh` 会激活 uv 管理的 `.venv` 并启动 Robyn 后端（不再启动 Ollama 或任何前端）：

```bash
chmod +x start.sh
./start.sh          # 执行 .venv 内解释器：python -m server --fast --disable-openapi
```

手动启动（等效）：

```bash
uv run python -m server
```

后端监听 **http://127.0.0.1:8080**，WebSocket 端点为 `/sessions/ws`。

### 5. （可选）桌面客户端
Tauri 2 + Nuxt 4 客户端位于 [client/](client/)，需要 Node.js 18+、pnpm 与 Rust：

```bash
cd client
pnpm install
pnpm dev          # 浏览器模式，开发服务器 http://localhost:3000
pnpm tauri dev    # 原生桌面模式
```

客户端默认连接 `http://127.0.0.1:8080` 的 Python 后端（可通过 `client/.env` 中的 `VITE_API_BACK_URL` 配置）。详见[客户端 README](client/README.md)。

---

## 📝 角色档案示例

Agent 的行为由 `workspace/` 下的文件驱动：

- **IDENTITY.md**：定义姓名、年龄、兴趣、人际关系等。
- **SOUL.md**：定义性格反差、语言风格与行为逻辑。
- **AGENTS.md**：定义工具使用优先级、安全边界与伦理准则。
- **USER.md**：存储用户相关的交互偏好与已知信息。
- **HEARTBEAT.md**：列出 Heartbeat 定时服务的待办任务。
- **character.json**：结构化角色配置（JSON）。
- **prompt_builder.py**：将档案文件构建为系统提示词。
- **file_sync.py**：按需从 `workspace/template/<lang>/`（由 `WORKSPACE_TEMPLATE_LANG` 选择）懒拷贝缺失的人设文件，且绝不覆盖用户修改。

---

## 🤝 参与贡献

欢迎提交 Issue 与 Pull Request！添加新技能的方法：

1. 在 `skills/` 下创建一个文件夹（第三方技能放在 `skills/plugins/`）。
2. 编写带 YAML frontmatter（`name`、`description`、可选 `scope`）的 `SKILL.md`，描述技能的用法与步骤。
3. 重启 Agent —— loader 会自动发现所有 `SKILL.md` 并暴露给模型。（也可以让运行中的 Agent 使用内置 `skill_creator` 技能自动生成。）

`skills/plugins/` 下的第三方技能会经过 SkillSpector 扫描，并保持停用直到被显式启用。

---

联系方式：QQ 3132225629

## 📄 许可证

本项目基于 MIT 许可证开源。

---

> **💡 提示**：本项目的灵感来自对先进 AI Agent 与深度角色扮演的探索。
