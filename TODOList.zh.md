# TODO List

## 1. 重写前端

使用 `client_future/` 文件夹下的 Tauri 2 + Nuxt 4 前端项目替代 `client/` 文件夹下的 Streamlit 前端。

### 架构

```
┌─────────────────┐     HTTP (localhost:XXX)     ┌──────────────────┐
│  Nuxt 4 (SPA)   │ ──────────────────────────▶  │ Python 后端      │
│  (app/)         │ ◀──────────────────────────  │ (FastAPI/Robyn)  │
│                 │     SSE / chunked JSON       │                  │
│  requestApi.ts  │                              │  server/         │
└────────┬────────┘                              └──────────────────┘
         │ Tauri Commands (IPC)
         ▼
┌─────────────────┐
│  Tauri Rust     │  ← 仅限系统级任务:
│  (src-tauri/)   │     - 文件读写（read/write text files）
│                 │     - 系统通知
│                 │     - 获取应用数据目录
│                 │     - 系统托盘 & 全局快捷键
│                 │     - 窗口控制
└─────────────────┘
```

**关键约束**: Tauri Rust 不做网络请求和业务逻辑。所有网络通信（包括 SSE 流式响应）都由 Nuxt 前端通过 `requestApi.ts` 直接请求 Python 后端。

### 子任务

#### 1.1 Tauri Rust 后端 (`src-tauri/`)

- [ ] 定义 Rust commands: `read_text_file`、`write_text_file`、`get_app_data_dir`、`show_notification`
- [ ] 更新 `tauri.conf.json`: productName → "EMA AI Agent", identifier → 实际 bundle ID, 添加系统托盘配置
- [ ] 添加 Tauri 插件: `tauri-plugin-notification`, `tauri-plugin-shell`（按需）
- [ ] 实现系统托盘菜单（显示/隐藏、退出）
- [ ] 实现全局快捷键（如 Alt+Space 切换窗口）
- [ ] 错误处理：通过 Tauri `Result<T, E>` 向前端返回结构化错误

#### 1.2 主页 — 会话侧边栏 (`app/pages/home/`)

- [ ] 会话列表：挂载时从后端 API 获取，展示在侧边栏
- [ ] 会话 CRUD：新建（通过后端）、删除、重命名
- [ ] 会话切换：点击侧边栏会话项 → 加载对应消息
- [ ] 响应式侧边栏：移动端（浮层）vs 桌面端（固定）
- [ ] 批量操作：多选会话、批量删除
- [ ] 搜索/筛选会话（按标题或日期范围）

#### 1.3 聊天视图 — 消息展示 (`app/pages/home/`)

- [ ] 消息气泡组件，支持 Markdown 渲染
- [ ] 区分用户消息和 AI 消息（左右对齐 + 样式差异）
- [ ] 消息元信息：时间戳、模型名、token 数
- [ ] 新消息自动滚动到底部
- [ ] AI 响应时的加载/流式指示器
- [ ] 支持多模态消息：文本 + 图片
- [ ] 复制消息文本按钮

#### 1.4 聊天输入框 (`app/components/chat/inputBox.vue`)

- [ ] 多行文本输入：Enter 发送 / Shift+Enter 换行
- [ ] 流式响应中发送按钮置为禁用状态
- [ ] 流式响应中的停止生成按钮
- [ ] 图片上传（multipart/form-data 通过 `requestApi.ts`）
- [ ] 文件上传附件
- [ ] 知识库开关 / 上下文模式选择器

#### 1.5 流式响应 & SSE 集成

- [ ] 消费 Python 后端 `/chat/stream` 端点的 SSE / chunked JSON 响应
- [ ] 逐块增量更新消息气泡内容
- [ ] 处理流取消（abort fetch + 通知后端）
- [ ] 处理网络错误重连

#### 1.6 本地状态管理 (`app/stores/`)

- [ ] Pinia store 管理会话列表（当前会话、CRUD 操作）
- [ ] Pinia store 管理消息列表（当前会话的消息、流式状态）
- [ ] Pinia store 管理 UI 状态（侧边栏展开、工具菜单、主题）
- [ ] Dexie.js (IndexedDB) 离线缓存：会话列表和最近消息
- [ ] 同步策略：先读 IndexedDB 展示 → 再向后端拉最新 → 更新缓存

#### 1.7 国际化 (`app/i18n/`)

- [ ] i18n JSON 文件: zh.json（已有骨架）、en.json、ja.json
- [ ] 翻译所有 UI 文案（侧边栏、输入框、按钮、提示、空状态）
- [ ] 确保多语言切换无缝工作

#### 1.8 Tauri 桌面集成

- [ ] 系统托盘图标 + 右键菜单（显示、隐藏、退出）
- [ ] 全局快捷键 Alt+Space 切换窗口显示/隐藏
- [ ] 单实例锁（防止多开窗口）
- [ ] 窗口状态持久化（位置、大小、最大化状态）

#### 1.9 打磨与测试

- [ ] 错误处理 UI: API 错误 toast 通知、离线提示
- [ ] 加载骨架屏 / spinner 组件
- [ ] 响应式布局：移动端、平板、桌面端测试
- [ ] 暗色模式一致性（所有组件的 tailwind `dark:` 类）
- [ ] 无障碍：键盘导航、焦点管理

---

## 2. 增加对接平台

在 channel 中增加对接平台,如 微信bot、飞书、X (Twitter)、Telegram

---

**[English Version](TODOList.md)**
