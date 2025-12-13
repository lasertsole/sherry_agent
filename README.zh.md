# 🍊 EMA AI Agent - 橘雪莉 (Sherry)

![Python](https://img.shields.io/badge/Python-3.12-blue)
![LangChain](https://img.shields.io/badge/LangChain-1.2-green)
![License](https://img.shields.io/badge/License-MIT-orange)

[**English**](README.md) | 中文文档

> **一个基于 LangGraph 与多模态技术的深度角色扮演 AI Agent。**

## ✨ 项目简介

EMA AI Agent 是一个高度拟人化、具备长期记忆与复杂推理能力的 AI 代理系统。它不仅仅是一个聊天机器人，更是一个拥有独立人格（Persona）、技能树（Skills）和动态记忆图谱（Skill Memory Graph）的虚拟伙伴。



---

## 🚀 核心功能特性

### 1. 🧠 深度记忆系统 (Context Engine)
- **Skill Memory Graph**: 自动从对话中提取知识点，构建动态知识图谱。
- **社区检测与摘要**: 定期对图谱进行社区划分并生成摘要，实现高效的长程记忆检索。
- **持久化存储**: 使用 SQLite 存储对话历史与记忆节点，支持跨会话的记忆继承。

### 2. 🛠️ 动态技能体系 (Dynamic Skills)
- **SKILL.md 规范**: 采用标准化的 Markdown 格式定义技能，Agent 可自主读取并学习新能力。
- **工具调用**: 内置 Web 搜索、文件读写、代码执行（Python Repl）、终端命令等丰富工具。
- **子代理 (Subagents)**: 支持在后台并行运行复杂的耗时任务，并通过消息总线异步反馈结果。

### 3. 🌐 多渠道接入 (Multi-Channel)
- **Web 界面**: 基于 Streamlit 构建的现代化聊天 UI，支持多模态输入（图片、语音）。
- **QQ 机器人**: 集成 `qq-botpy`，可直接在 QQ 群组或个人聊天中互动。
- **消息总线**: 内部采用异步消息队列（MessageBus）解耦输入输出通道。

### 4. 🔊 多模态交互
- **TTS 语音合成**: 集成 GPT-SoVITS，支持高度还原角色声线的实时语音回复。
- **视觉理解**: 支持 VL 模型（Vision-Language），能够识别并分析用户上传的图片内容。

---

## 🏗️ 技术架构

本项目基于 **Python 3.12** 构建，核心技术栈包括：

| 模块 | 技术选型 |
| :--- | :--- |
| **Agent 框架** | LangChain 1.2, LangGraph |
| **向量与检索** | FAISS, LightRAG, Sentence Transformers |
| **数据库** | SQLite (FTS5 全文检索), LanceDB |
| **Web 服务** | Robyn (高性能异步服务器) |
| **前端 UI** | Streamlit, Nuxt.js + Tauri (客户端) |
| **LLM 支持** | DeepSeek, OpenAI, Ollama (本地模型) |

---

## 📂 项目结构

```text
EMA_AI_agent/
├── agent/              # Agent 核心逻辑与中间件（如 Summarization）
├── context_engine/     # 记忆引擎（MesMemory & Skill Memory Graph）
├── skills/             # 技能库（SKILL.md 定义文件）
├── tools/              # 工具集（Web Search, Python Repl, Agentic RAG 等）
├── channels/           # 渠道适配器（QQ, WebSocket）
├── client/             # Streamlit 前端入口
├── server/             # Robyn 后端服务与 API 路由
├── workspace/          # 角色设定文件（IDENTITY.md, SOUL.md, USER.md）
├── models/             # 模型封装（Chat, Reasoner, VL, TTS）
└── pub_func/           # 公共工具函数
```

---

## ⚡ 快速开始

### 1. 环境准备
确保已安装 **Python 3.12+**。

```bash
git clone https://github.com/your-repo/EMA_AI_agent.git
cd EMA_AI_agent
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
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
streamlit run client/core.py  # 启动前端
```

---

## 📝 角色设定示例

Agent 的行为逻辑由 `workspace/` 下的 Markdown 文件驱动：

*   **IDENTITY.md**: 定义姓名、年龄、兴趣、人际关系等基础档案。
*   **SOUL.md**: 定义人格反差、语言风格（如 "~です"）及行为逻辑。
*   **AGENTS.md**: 定义工具使用优先级、安全边界及道德准则。

---

## 🤝 贡献指南

欢迎提交 Issue 或 Pull Request！如果你想增加新的技能：
1. 在 `skills/` 目录下创建文件夹。
2. 编写 `SKILL.md` 描述技能的使用场景与步骤。
3. 重启 Agent，它将自动发现并加载新技能。

---

## 📄 许可证

本项目采用 MIT 许可证。

---

> **💡 提示**：本项目灵感来源于对高级 AI 代理与深度角色扮演的探索。
