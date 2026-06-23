---
layout: home
hero:
  name: "EMA AI Agent"
  text: "后端 API 文档"
  tagline: 面向前端开发者的 Tauri IPC 命令参考
  actions:
    - theme: brand
      text: 快速开始
      link: /zh/guide/getting-started
    - theme: alt
      text: 命令参考
      link: /zh/commands/agent

features:
  - title: 12 个 IPC 命令
    details: Agent 对话/停止、会话管理、系统提示词、角色配置和系统工具。全部通过 HTTP 桥接代理到 Python 后端。
  - title: 流式事件
    details: 通过 Tauri 事件系统实时推送 Agent 响应。Python 后端的 SSE 流被转发为 4 种 Tauri 事件。
  - title: TypeScript 类型
    details: 通过 ts-rs 从 Rust 自动生成 .d.ts 类型定义，编译时类型安全。
  - title: 混合架构
    details: Tauri/Rust HTTP 桥接代理所有请求到 Python 后端 (LangGraph, RAG, TTS)。Rust 层零业务逻辑。
  - title: 桌面功能
    details: 系统托盘、Alt+Space 全局快捷键、单实例锁、窗口状态持久化。
  - title: 错误处理
    details: 12 种结构化错误码 (含 BACKEND_ERROR)，支持重试提示和前端友好的错误信息。
---
