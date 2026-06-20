# 🍊 EMA AI Agent - 橘雪莉

![Python](https://img.shields.io/badge/Python-3.13-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.3+-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**English**](README.md) | 中文文档

> **一个基于 LangGraph 与多模态技术的深度角色扮演 AI Agent。**

## ✨ 项目简介

EMA AI Agent 是一个高度拟人化、具备长期记忆与复杂推理能力的 AI 代理系统。它不仅仅是一个聊天机器人，更是一个拥有独立人格（Persona）、动态记忆图谱（Skill Memory Graph）和主动行为能力的虚拟伙伴。

该 Agent 的角色**橘雪莉** 是一位侦探少女，具有人格反差（温柔/冷淡双模式），会根据用户亲密度切换状态。整个系统旨在支持沉浸式的、记忆跨会话持续积累的角色扮演体验。

---

## 🚀 核心功能特性

### 1. 🧠 深度记忆系统 (Context Engine)
- **双记忆架构**：短期对话记忆 [MesMemory](context_engine/mes_memory/README.zh.md) + 长期知识图谱 [Skill Memory](context_engine/skill_memory/README.zh.md)
- **Skill Memory Graph**：自动从对话中提取知识点，构建动态知识图谱
- **社区检测与摘要**：定期对图谱进行社区划分并生成摘要，实现高效的长程记忆检索
- **查询重写**：在检索前利用对话历史消除代词和模糊引用
- **持久化存储**：使用 SQLite + FTS5 存储对话历史与记忆节点，支持跨会话的记忆继承
- **异步非阻塞提取**：Skill Memory 提取在后台运行，MesMemory 写入为同步即时操作
- ▶️ _详见 [Context Engine 文档](context_engine/README.zh.md)_

### 2. 🛠️ 动态技能体系 (Dynamic Skills)
- **SKILL.md 规范**：采用标准化的 Markdown 格式定义技能，Agent 可自主读取并学习新能力
- **工具调用**：内置 Web 搜索、文件读写、代码执行（Python Repl）、终端命令、消息搜索等多种工具
- **子代理 (Subagents)**：支持在后台并行运行复杂的耗时任务，并通过消息总线异步反馈结果
- ▶️ _详见 [Subagent 系统文档](tools/subagent/README.zh.md)_

### 3. 🌐 多渠道接入 (Multi-Channel)
- **Web 界面**：基于 Streamlit 构建的现代化聊天 UI，支持多模态输入（图片、语音）
- **下一代客户端** ([client_future](client_future/README.zh.md))：基于 Tauri 2 + Nuxt 4 的桌面/移动 SPA 客户端，正在开发中
- **QQ 机器人**：集成 `qq-botpy`，可直接在 QQ 群组或个人聊天中互动
- **消息总线**：内部采用异步消息队列 [MessageBus](bus/queue.py) 解耦输入输出通道

### 4. 🔊 多模态交互
- **TTS 语音合成**：外部集成 GPT-SoVITS，支持高度还原角色声线的实时语音回复
- **视觉理解**：支持 VL 模型（Image-to-Text），能够识别并分析用户上传的图片内容

### 5. ⏰ 定时与主动行为
- **定时任务服务** ([cron/](skills/builtin/core/cron/scripts/README.zh.md))：支持一次性、固定间隔和 Cron 表达式三种调度类型的 Agent 任务执行
- **心跳服务** ([heartbeat/](skills/builtin/core/heartbeat/README.zh.md))：定期唤醒 Agent，检查 HEARTBEAT.md 中待处理的任务并在空闲时自动执行
---

## 🏗️ 技术架构

本项目基于 **Python 3.13** 构建，核心技术栈包括：

| 模块 | 技术选型 |
| :--- | :--- |
| **Agent 框架** | LangChain 1.3+, langchain-classic, LangGraph |
| **向量与检索** | FAISS, LightRAG, Sentence Transformers, BGE/BAAI Embedding 系列 |
| **数据库** | SQLite (FTS5 全文检索), LanceDB |
| **图算法** | igraph + Leiden 算法（社区检测）, PageRank |
| **Web 服务** | Robyn + FastAPI (双异步服务器) |
| **前端 UI** | Streamlit, Tauri 2 + Nuxt 4 (下一代客户端) |
| **LLM 支持** | DeepSeek, OpenAI, Ollama (本地模型), langchain-deepseek |
| **任务调度** | croniter, asyncio |
| **异步消息** | asyncio.Queue (MessageBus) |

---

## 📂 项目结构

```text
EMA_AI_agent/
├── agent/                  # Agent 核心逻辑与中间件
│   ├── core.py             # 主 Agent 循环（LangGraph 编译图）
│   ├── middlewares/        # 6 个中间件：ContextEngineHook, Summarization,
│   │                       #   ToolLoopPrevention, ToolCallNormalize,
│   │                       #   ToolTimeout, MultimodalProcessor
│   └── checkpointer/       # 会话状态检查点
│
├── bus/                    # 消息总线（异步队列）
│   ├── queue.py            # MessageBus — 入站/出站队列
│   └── events.py           # InboundMessage, OutboundMessage 数据模型
│
├── channels/               # 渠道适配器（QQ, WebSocket）
├── client/                 # Streamlit 前端入口
├── client_future/          # 下一代客户端 (Tauri 2 + Nuxt 4)
│   ├── app/                # Nuxt 4 SPA 源码
│   │   ├── app.vue                  # 根组件入口
│   │   ├── common.scss              # 全局 SCSS 混合库
│   │   ├── assets/
│   │   │   ├── css/
│   │   │   │   ├── main.css         # 全局 CSS 重置 + CSS 变量
│   │   │   │   ├── main.scss        # (保留)
│   │   │   │   └── tailwind.scss    # Tailwind 指令
│   │   │   └── ts/
│   │   │       └── tailwind.config.ts # Tailwind 自定义令牌
│   │   ├── composables/             # Vue 3 组合式逻辑
│   │   │   └── mitt.ts              # mitt 事件总线实例
│   │   ├── declare/                 # (保留) 类型声明
│   │   │   └── declarations.d.ts
│   │   ├── layouts/
│   │   │   └── default.vue          # 默认布局入口
│   │   ├── pages/
│   │   │   └── index.vue            # 主页面
│   │   ├── nuxt.config.ts           # Nuxt 4 配置
│   │   ├── package.json             # 依赖清单
│   │   └── tsconfig.json            # TypeScript 配置
│   ├── src-tauri/                   # Tauri 2 原生壳
│   │   ├── capabilities/
│   │   │   └── default.json         # 权限配置
│   │   ├── src/
│   │   │   ├── lib.rs               # Tauri 应用入口
│   │   │   └── main.rs              # Windows 子系统入口
│   │   ├── Cargo.toml               # Rust 依赖
│   │   ├── tauri.conf.json          # Tauri 2 配置
│   │   └── build.rs                 # Tauri 构建脚本
│   ├── README.md                    # 英文文档
│   └── README.zh.md                 # 中文文档
│
├── config/                 # 集中配置管理
│   ├── path.py             # 文件路径配置
│   ├── schema.py           # 配置模式模型
│   ├── character.py        # 角色档案配置
│   └── num.py              # 数值/调优参数
│
├── context_engine/         # 记忆引擎 — 查看 README 获取完整文档
│   ├── pre_builder.py      # 统一预构建 API（查询重写 + 记忆组装）
│   ├── mes_memory/         # 短期会话消息记忆 (SQLite + FTS5)
│   │   ├── core.py         # 业务逻辑：检索、搜索、nudge 提取
│   │   └── store/          # 数据层：SQLite CRUD、迁移
│   └── skill_memory/       # 长期知识图谱
│       ├── core.py         # 编排器：assemble, ingest, after_turn
│       ├── extractor/      # 基于 LLM 的节点/边从对话中提取
│       ├── recaller/       # 双路召回（精确 + 泛化）
│       ├── graph/          # 社区检测 (Leiden)、PageRank、去重
│       └── store/          # SQLite + 向量 + FTS5 存储
│
├── cron/                   # 定时任务服务 — 查看 README
│   ├── core.py             # CronService：定时循环、任务执行
│   ├── types.py            # CronSchedule, CronPayload, CronJob 模型
│   └── jobs.json           # 持久化任务存储
│
├── heartbeat/              # 心跳任务检查服务 — 查看 README
│   ├── core.py             # HeartbeatService：循环、LLM 决策、执行
│   └── evaluate.py         # 通知门控：判断结果是否值得推送
│
├── models/                 # 模型封装
│   ├── chat_model.py       # 聊天模型 (LangChain BaseChatModel)
│   ├── simple_chat_model.py # 轻量聊天模型
│   ├── reasoner_model.py   # 推理模型 (思维链)
│   ├── VTTT_model.py       # 视频-文本-文本模型
│   ├── ITT_model.py        # 图像-文本模型
│   ├── SST_model/          # 语音-文本模型
│   ├── embed_model/        # 文本嵌入模型
│   ├── reranker_model/     # 交叉编码重排序器
│   ├── extract_model/      # 实体提取模型
│   └── sovits_model/       # TTS 语音合成 (GPT-SoVITS)
│
├── pub_func/               # 公共工具函数
├── rag/                    # 检索增强生成模块
│   ├── agentic_rag/        # Agentic RAG（工具调用编排）
│   ├── mutil_hop_graphrag/ # 图知识多跳推理
│   └── rag_anything/       # 通用 RAG 适配器
│
├── runtime/                # 运行时工具
│   ├── count_register.py   # 使用量/统计计数器
│   └── relation_register.py# 关系/亲密度追踪
│
├── server/                 # Robyn 后端服务与 API 路由
├── sessions/               # 会话运行时数据（每个会话独立）
├── skills/                 # 技能库（SKILL.md 定义文件）
│   ├── loader.py           # 技能自动发现与注册
│   ├── skills_snapshot.py  # 构建技能提示快照
│   └── builtin/            # 内置技能实现
│       ├── core/           # 核心内置技能
│       │   ├── web_search/     # 网页搜索与抓取
│       │   ├── python_repl/    # Python 代码执行
│       │   ├── terminal/       # 终端命令执行
│       │   ├── image_to_text/  # 图像理解
│       │   ├── speech_to_text/ # 语音识别
│       │   ├── video_text_to_text/ # 视频理解
│       │   ├── rag/            # RAG 知识检索
│       │   ├── clawhub/        # GitHub 仓库克隆
│       │   └── skill_creator/  # 自动生成新技能
│       └── text_to_image/  # 图像生成
├── src/                    # 运行时数据目录
│   ├── avatar/             # 角色头像图片
│   ├── checkpoints/        # 会话检查点
│   ├── gallery/            # 图片库
│   ├── rag/                # RAG 索引数据
│   ├── sessions/           # 会话存储
│   ├── store/              # 数据存储
│   └── temp/               # 临时文件
│
├── subagent/               # 子代理系统 — 查看 README
│   ├── core.py             # SubagentManager（单例编排器）
│   ├── commander/          # 基于 LangGraph 的 Commander Agent
│   │   ├── core.py         # build_commander() — 图构建
│   │   ├── tools/          # TodoWriter, Worker（并行派发）
│   │   └── middlewares/    # TodoInjector, TodoCleaner, SummarizationMiddleware
│   ├── templates/          # 结果公告模板 (Jinja2 风格)
│   └── type.py             # SubAgentOutput 数据模型
│
├── tests/                  # 测试套件
├── future/                 # 未来可能上线的新功能
├── tools/                  # Agent 可访问的工具
│   ├── web_search.py       # 网页搜索工具
│   ├── python_repl.py      # Python 代码执行
│   ├── terminal.py         # 终端命令执行
│   ├── read_file.py        # 文件读取
│   ├── write_file.py       # 文件写入
│   ├── subagent.py         # 子代理生成工具 (SubagentTool)
│   ├── cron.py             # 定时任务管理工具
│   ├── memory.py           # 记忆检查工具
│   └── message_search.py   # 对话搜索工具
│
├── type/                   # 共享数据模型
│   ├── __init__.py         # MultiModalMessage, Chat, FileType 等
│   └── ...                 # Pydantic v2 模型
│
├── workspace/              # 角色设定文件 (IDENTITY.md, SOUL.md, USER.md)
├── output/                 # 输出目录（生成文件）
├── start.sh                # 一键启动脚本
├── pyproject.toml          # Python 依赖（uv 管理）
└── .env                    # 环境变量 (API Key, 模型路径)

```

---

## 📚 子模块文档

每个主要子系统都有详细的 README 文档：

| 子模块 | 描述 | 文档 |
|-------|------|------|
| **Context Engine** | 双记忆系统（MesMemory + Skill Memory） | [中文](context_engine/README.zh.md) · [EN](context_engine/README.md) |
| **MesMemory** | 短期会话消息记忆 | [中文](context_engine/mes_memory/README.zh.md) · [EN](context_engine/mes_memory/README.md) |
| **Skill Memory** | 长期知识图谱记忆 | [中文](context_engine/skill_memory/README.zh.md) · [EN](context_engine/skill_memory/README.md) |
| **Subagent System** | 层次化任务分解与并行执行 | [中文](tools/subagent/README.zh.md) · [EN](tools/subagent/README.md) |
| **Cron Service** | 定时/周期 Agent 任务执行 | [中文](skills/builtin/core/cron/scripts/README.zh.md) · [EN](skills/builtin/core/cron/scripts/README.md) |
| **Heartbeat Service** | 定时唤醒任务检查 | [中文](skills/builtin/core/heartbeat/README.zh.md) · [EN](skills/builtin/core/heartbeat/README.md) |
| **Next-gen Client** | Tauri 2 + Nuxt 4 桌面/移动 SPA 客户端 | [中文](client_future/README.zh.md) · [EN](client_future/README.md) |

---

## ⚡ 快速开始

### 1. 环境准备
确保已安装 **Python 3.13+**。

```bash
git clone https://github.com/your-repo/EMA_AI_agent.git
cd EMA_AI_agent
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
uv sync
```

### 2. 模型下载说明
首次运行时，系统会自动从 **Hugging Face** 下载 Embedding 模型和 Reranker 模型到 `models/embed_model` 与 `models/reranker_model` 目录。请确保：

- **网络环境**：能够正常访问 huggingface.co（国内用户请配置代理或使用镜像站）。
- **耐心等待**：模型权重文件较大（数百 MB 至数 GB），下载耗时取决于你的网络速度。
- **断点续传**：若下载中断，删除对应目录后重新启动即可重新下载。

> 你也可以手动下载模型放入对应目录以跳过自动下载流程。

### 3. 配置环境变量
复制 `.env` 示例并填写你的 API Key（DeepSeek, OpenAI 等）及 TTS 模型路径。

```bash
cp .env.example .env
# 编辑 .env 文件，配置 CHAT_API_KEY, GPT_SOVITS_DIR 等
```

### 4. 启动服务
使用提供的 `start.sh` 脚本一键启动 TTS、Ollama 本地模型、后端服务及前端 UI。

```bash
chmod +x start.sh
./start.sh
```

### 5. （可选）手动启动各组件

你也可以逐个手动启动：
```bash
python -m server  # 启动后端
streamlit run client/base.py  # 启动前端
```

---

## 📝 角色设定示例

Agent 的行为逻辑由 `workspace/` 下的 Markdown 文件驱动：

- **IDENTITY.md**: 定义姓名、年龄、兴趣、人际关系等基础档案。
- **SOUL.md**: 定义人格反差、语言风格（如 "~です"）及行为逻辑。
- **AGENTS.md**: 定义工具使用优先级、安全边界及道德准则。
- **USER.md**: 存储用户特定的交互偏好和已知事实。

---

## 🤝 贡献指南

欢迎提交 Issue 或 Pull Request！如果你想增加新的技能：

1. 在 `skills/` 目录下创建文件夹。
2. 编写 `SKILL.md` 描述技能的使用场景与步骤。
3. 重启 Agent，它将自动发现并加载新技能。

---

联系方式: QQ 3132225629

## 📄 许可证

本项目采用 MIT 许可证。

---

> **💡 提示**：本项目灵感来源于对高级 AI 代理与深度角色扮演的探索。
