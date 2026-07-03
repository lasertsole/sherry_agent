# 🍊 EMA AI Agent - 橘雪莉

![Python](https://img.shields.io/badge/Python-3.13-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.3+-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**English**](README.md) | 中文文档

> **一个基于 LangGraph 与多模态技术的深度角色扮演 AI Agent。**

## ✨ 项目简介

EMA AI Agent 是一个高度拟人化、具备长期记忆与复杂推理能力的 AI 代理系统。它不仅仅是一个聊天机器人，更是一个拥有独立**人格（Persona）**、动态**记忆图谱（Skill Memory Graph）**和主动行为能力的虚拟伙伴。

该 Agent 的角色**橘雪莉**是一位侦探少女，具有人格反差（温柔/冷淡双模式），会根据用户亲密度切换状态。整个系统旨在支持沉浸式的、记忆跨会话持续积累的角色扮演体验。

---

## 🚀 核心功能特性

### 1. 🧠 深度记忆系统 (Context Engine)
- **双记忆架构**：短期对话记忆 [MesMemory](context_engine/README.zh.md) + 长期知识图谱 [Skill Memory](context_engine/skill_memory/README.zh.md)
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
- **QQ 机器人**：通过插件系统集成 QQ 频道适配器（`plugins/channels/`）
- **消息总线**：内部采用异步消息队列 [MessageBus](bus/core.py) 解耦输入输出通道

### 4. 👁️ 多模态交互
- **视觉理解**：支持 Image-to-Text（VL）模型，能够识别并分析用户上传的图片内容

### 5. ⏰ 定时与主动行为
- **定时任务服务** ([cron/](skills/builtin/core/cron/scripts/README.zh.md))：支持一次性、固定间隔和 Cron 表达式三种调度类型的 Agent 任务执行
- **心跳服务** ([heartbeat/](skills/builtin/core/heartbeat/README.zh.md))：定期唤醒 Agent，检查 HEARTBEAT.md 中待处理的任务并在空闲时自动执行

---

## 🏗️ 核心技术栈

基于 **Python 3.13**，核心依赖如下：

| 模块 | 技术 |
| :--- | :--- |
| **Agent 框架** | LangChain 1.3+, langchain-classic, LangGraph |
| **向量检索** | FAISS, LightRAG, Sentence Transformers, BGE/BAAI Embedding 系列 |
| **数据库** | SQLite（FTS5 全文检索）, LanceDB |
| **图算法** | igraph + Leiden 算法（社区检测）, PageRank |
| **Web 服务** | Robyn + FastAPI（双异步服务） |
| **前端 UI** | Streamlit, Tauri 2 + Nuxt 4（下一代客户端） |
| **LLM 支持** | DeepSeek, OpenAI, Ollama（本地模型）, langchain-deepseek |
| **任务调度** | croniter, asyncio |
| **异步消息** | asyncio.Queue（MessageBus） |

---

## 📂 项目目录结构

```text
EMA_AI_agent/
├── agent/                  # Agent 核心逻辑与中间件
│   ├── core.py             # 主 Agent 循环（LangGraph 编译图）
│   ├── middlewares/        # 中间件：Summarization, ToolCallNormalize,
│   │                       #   ToolGuardrails, ToolTimeout, IterationBudget,
│   │                       #   MultimodalProcessor, ContextEngineHook
│   └── checkpointer/       # 会话状态检查点
│
├── bus/                    # 消息总线（异步队列）
│   └── core.py             # MessageBus — 入站/出站队列与事件
│
├── channels/               # 通道接口定义
│   ├── base.py             # 抽象通道基类
│   ├── manager.py          # 通道生命周期管理器
│   └── registry.py         # 通道注册
│
├── client/                 # Streamlit 前端入口
│   ├── api/                # API 客户端层
│   └── core.py             # Streamlit 应用入口
│
├── client_future/          # 下一代客户端（Tauri 2 + Nuxt 4）
│   ├── app/                # Nuxt 4 SPA 源码
│   │   ├── app.vue         # 根组件入口
│   │   ├── pages/          # 页面组件
│   │   ├── layouts/        # 布局组件
│   │   ├── composables/    # Vue 3 组合式逻辑
│   │   ├── assets/         # CSS 与配置资源
│   │   ├── nuxt.config.ts  # Nuxt 4 配置
│   │   └── package.json    # 依赖清单
│   ├── src-tauri/          # Tauri 2 原生壳（Rust）
│   │   ├── src/            # Rust 源码
│   │   ├── Cargo.toml      # Rust 依赖
│   │   └── tauri.conf.json # Tauri 2 配置
│   └── README.md           # 英文文档
│
├── config/                 # 集中配置
│   ├── path.py             # 文件路径配置
│   ├── schema.py           # 配置模式定义
│   ├── character.py        # 角色配置
│   └── num.py              # 数值/调参配置
│
├── context_engine/         # 记忆引擎
│   ├── pre_builder.py      # 统一预处理 API（查询重写 + 记忆组装）
│   ├── mes_memory/         # 短期会话消息记忆（SQLite + FTS5）
│   └── skill_memory/       # 长期知识图谱
│       ├── core.py         # 编排器：组装、写入、after_turn
│       ├── extractor/      # LLM 从对话中提取节点/边
│       ├── recaller/       # 双路径召回（精确 + 泛化）
│       ├── graph/          # 社区检测（Leiden）、PageRank、去重
│       └── store/          # SQLite + 向量 + FTS5 存储
│
├── future/                 # 实验性/即将推出的功能
│
├── logs/                   # 日志系统
│   ├── logger.py           # 日志配置
│   └── output/             # 日志输出目录
│
├── models/                 # 模型封装
│   ├── chat_model.py       # 聊天模型（LangChain BaseChatModel）
│   ├── LLMs/               # LLM 模型配置
│   │   ├── auxiliary_llm.py    # 轻量聊天模型
│   │   ├── main_llm.py         # 主聊天模型
│   │   └── reasoner_llm.py     # 思维链推理模型
│   ├── VTTT_model.py       # 视频转文本模型
│   ├── ITTT_model.py        # 图片转文本模型
│   ├── STT_model/          # 语音转文本模型
│   ├── embed_model/        # 文本嵌入模型
│   ├── reranker_model/     # 交叉编码重排序模型
│   └── extract_model/      # 实体提取模型
│
├── plugins/                # 插件系统
│   ├── channels/           # 通道插件（QQ 机器人等）
│   └── mcp_server/         # MCP 服务器配置
│
├── pub_func/               # 公用工具函数
│   ├── format/             # 文本格式化工具
│   ├── media/              # 媒体处理工具
│   ├── message/            # 消息处理工具
│   └── validator/          # 输入验证工具
│
├── runtime/                # 运行时状态与工具
│   ├── core.py             # 运行时生命周期
│   ├── _callback_executor.py   # 异步回调执行器
│   ├── count_call_register.py   # 用量/统计计数器
│   ├── relation_register.py    # 关系/亲密度追踪
│   ├── state_register.py   # 状态注册表
│   └── timer_call_register.py   # 计时器注册表
│
├── server/                 # Robyn 后端服务与 API 路由
│   ├── DAO/                # 数据访问层
│   ├── service/            # 业务逻辑层
│   └── trigger/            # 触发器管理器
│       ├── channels/       # 入站通道触发器
│       ├── http/           # HTTP 端点触发器
│       └── subagent/       # 子代理结果触发器
│
├── sessions/               # 会话管理
│   ├── main/               # 活跃会话存储
│   └── store.py            # 会话 CRUD 操作
│
├── skills/                 # 技能库（SKILL.md 定义文件）
│   ├── loader.py           # 技能自动发现与注册
│   ├── skills_snapshot.py  # 构建技能提示快照
│   └── builtin/            # 内置技能实现
│       └── core/           # 核心内置技能
│           ├── web_search/     # 网络搜索与抓取
│           ├── cron/           # 定时任务技能
│           ├── heartbeat/      # 心跳检测技能
│           ├── image_to_text/  # 图片理解
│           ├── speech_to_text/ # 语音识别
│           ├── video_text_to_text/ # 视频理解
│           ├── multimodal_rag/ # RAG 知识检索
│           ├── clawhub/        # GitHub 仓库克隆
│           └── skill_creator/  # 自动生成新技能
│
├── src/                    # 运行时数据目录
│   ├── checkpoints/        # 会话检查点
│   ├── data/               # 数据存储
│   ├── gallery/            # 图片画廊
│   ├── rag/                # RAG 索引数据
│   ├── sessions/           # 会话运行时存储
│   └── store/              # 数据存储
│
├── static/                 # 静态资源
│   ├── avatar/             # 角色头像
│   └── images/             # 其他图片
│
├── temp/                   # 临时文件
│
├── tests/                  # 测试套件
│
├── tools/                  # Agent 可调用的工具
│   ├── subagent/           # 子代理系统
│   │   ├── core.py         # SubagentManager（单例编排器）
│   │   ├── commander/      # 基于 LangGraph 的 Commander 代理
│   │   ├── templates/      # 结果通告模板
│   │   └── type.py         # SubAgentOutput 数据模型
│   ├── mcp_plugin.py       # MCP 插件工具
│   ├── web_search.py       # 网络搜索工具
│   ├── python_repl.py      # Python 代码执行
│   ├── terminal.py         # 终端命令执行
│   ├── read_file.py        # 文件读取
│   ├── write_file.py       # 文件写入
│   ├── memory.py           # 记忆检查工具
│   ├── message_search.py   # 对话搜索工具
│   └── cron.py             # 定时任务管理工具（已废弃）
│
├── type/                   # 共享数据模型
│   ├── message.py          # MultiModalMessage, Chat 等
│   ├── bus.py              # 消息总线数据模型
│   └── client.py           # 客户端数据模型
│
├── workspace/              # 角色配置与行为定义
│   ├── IDENTITY.md         # 姓名、年龄、兴趣、关系等
│   ├── SOUL.md             # 人格反差、说话风格
│   ├── AGENTS.md           # 工具使用优先级、安全边界
│   ├── USER.md             # 用户偏好与已知事实
│   ├── HEARTBEAT.md        # 心跳服务的待处理任务
│   ├── character.json      # 角色配置 JSON
│   ├── prompt_builder.py   # 角色文件转提示词构建器
│   ├── template/           # 提示词模板
│   └── memory/             # 长期记忆存储
│
├── .env                    # 环境变量（API Key、模型路径等）
├── .env.example            # 环境变量模版
├── pyproject.toml          # Python 依赖（uv 管理）
├── start.sh                # 一键启动脚本
└── TODOList.md             # 开发路线图
```

---

## 📚 子模块文档

各主要子系统均配有独立的详细 README：

| 子模块 | 说明 | 文档 |
|--------|------|------|
| **Context Engine** | 双记忆系统（MesMemory + Skill Memory） | [中文](context_engine/README.zh.md) · [英文](context_engine/README.md) |
| **MesMemory** | 短期会话消息记忆 | [中文](context_engine/README.zh.md) · [英文](context_engine/README.md) |
| **Skill Memory** | 长期知识图谱记忆 | [中文](context_engine/skill_memory/README.zh.md) · [英文](context_engine/skill_memory/README.md) |
| **Subagent 系统** | 层级式任务分解与并行执行 | [中文](tools/subagent/README.zh.md) · [英文](tools/subagent/README.md) |
| **Cron 服务** | 定时/周期 Agent 任务执行 | [中文](skills/builtin/core/cron/scripts/README.zh.md) · [英文](skills/builtin/core/cron/scripts/README.md) |
| **Heartbeat 服务** | 周期性唤醒任务检查 | [中文](skills/builtin/core/heartbeat/README.zh.md) · [英文](skills/builtin/core/heartbeat/README.md) |
| **下一代客户端** | Tauri 2 + Nuxt 4 桌面/移动 SPA 客户端 | [中文](client_future/README.zh.md) · [英文](client_future/README.md) |
| **Channels** | 通道接口与适配器系统 | [中文](channels/README.zh.md) · [英文](channels/README.md) |

---

## ⚡ 快速启动

### 1️⃣ 环境准备
确保已安装 **Python 3.13+**。

```bash
git clone https://github.com/your-repo/EMA_AI_agent.git
cd EMA_AI_agent
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
uv sync
```

### 2️⃣ 模型下载
首次启动系统会自动从 **Hugging Face** 下载 Embedding 模型和 Reranker 模型至 `models/embed_model` 和 `models/reranker_model`。请注意：

- **网络**：确保能够访问 huggingface.co（国内用户可能需要代理或镜像）。
- **耐心等待**：模型权重较大（几百 MB 到几个 GB），下载速度取决于你的网络状况。
- **中断重试**：若下载中断，删除对应目录重新启动即可重新下载。

> 你也可以手动下载模型并放置在对应目录中，以跳过自动下载。

### 3️⃣ 配置环境变量
复制 `.env` 示例文件并填写你的 API Key（DeepSeek、OpenAI 等）和模型路径。

```bash
cp .env.example .env
# 编辑 .env 配置 MAIN_LLM_API_KEY、模型路径等
```

### 4️⃣ 启动服务
使用 `start.sh` 一键启动本地 Ollama 模型、后端服务和前端 UI。

```bash
chmod +x start.sh
./start.sh
```

### 5️⃣ 手动启动（可选）

你也可以手动逐个启动各组件：

```bash
python -m server              # 启动后端服务
streamlit run client/core.py  # 启动前端界面
```

---

## 📝 角色配置示例

Agent 的行为由 `workspace/` 目录下的 Markdown 文件驱动：

- **IDENTITY.md**：定义姓名、年龄、兴趣、人际关系等
- **SOUL.md**：定义人格反差、说话风格与行为逻辑
- **AGENTS.md**：定义工具使用优先级、安全边界与伦理准则
- **USER.md**：存储用户特定的交互偏好与已知事实
- **HEARTBEAT.md**：列出定时心跳服务待处理的任务
- **character.json**：结构化角色配置（JSON）

---

## 🤝 参与贡献

欢迎提交 Issue 与 Pull Request！添加新技能的方式：

1. 在 `skills/` 下创建文件夹
2. 编写 `SKILL.md` 描述技能的用法与步骤
3. 重启 Agent — 它会自动发现并加载新技能

---

联系方式：QQ 3132225629

## 📄 开源协议

本项目基于 MIT 协议开源。

---

> **💡 提示**：本项目受 AI Agent 前沿探索与深度角色扮演的启发。
