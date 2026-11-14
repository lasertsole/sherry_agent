# 计划：临时系统信息协议（system.notice）— 进对话流但不入库

- 日期：2026-11-12
- 状态：**待评审**
- 来源：借鉴同类工具的 system.notice 机制；Sherry 现有 system-reminder（入库+进历史）和 WS 帧（不入库但只进侧面板），缺"进 chat 对话流但不入库"的第三种消息
- 风险等级：**低** —— 新增一个 WS 帧 type + 前端临时消息列表，不碰 MesMemory/checkpoint/transcript

---

## 0. 现状缺口（已核实）

Sherry 有两种"非用户非 AI"信息机制，都不完全等于 system.notice：

| 机制 | 入库 | 进 chat 流 | 给谁看 |
|---|---|---|---|
| **system-reminder**（`DynamicReminderMiddleware`） | 是（进 MesMemory + checkpoint + 历史） | 是（折叠卡片） | 模型 + 用户 |
| **WS 广播帧**（safety_filter/todo/taskflow progress/terminal/web_search） | 否 | **否**（驱动侧面板，不进 chat 流） | 用户（侧面板） |
| **system.notice（缺）** | **否** | **是**（chat 流临时卡片） | 用户 |

**缺口**：外部工具/CI/插件想把"构建完成""工具执行中""插件提示"反馈进 chat 对话流，又不污染历史——Sherry 没有这个。要么入库（reminder，污染历史），要么不进 chat（WS 帧到侧面板）。

---

## 1. 设计

### 1.1 新 WS 帧 type：`system.notice`

```json
{
  "type": "system.notice",
  "data": {
    "title": "构建完成",
    "content": "前端构建成功，输出在 dist/",
    "level": "success",
    "source": "ci"
  }
}
```

- `level`：`info` / `success` / `warning` / `error` / `progress`
- `source`：来源标识（`ci` / `plugin:<name>` / `tool:<name>` / `external`）
- **不入 MesMemory、不进 checkpoint、不进 transcript、不进历史**
- 只进前端**临时消息列表**，渲染为 chat 流里的临时卡片

### 1.2 前端临时消息列表

- `client/app/composables/` 新增 `use-system-notice.ts`：进程级 reactive 列表 `systemNotices: ref<SystemNotice[]>([])`
- WS 收到 `system.notice` 帧 → push 进列表
- ChatBox 渲染：在对应位置插入临时卡片（居中、带 level 颜色、source 标签），**不从 history API 来**（history 不含 notice）
- 生命周期：notice 在前端存活到 turn 结束或用户关闭；刷新页面后消失（不入库所以不恢复）
- 长内容默认折叠（标题 + 展开看正文）

### 1.3 外部推送入口

- **WS 推送**：外部工具/CI 通过 session WS 发 `system.notice` 帧（需 session WS 连接 + 鉴权）
- **HTTP 推送**（可选）：`POST /sessions/:sid/notice`（body = notice data），服务端转 WS 帧广播给 session 的所有 socket。让无 WS 连接的工具（CI webhook）也能推
- **内部推送**：agent 中间件/工具也可调 `push_system_notice(session_id, notice)` 推（如工具执行中/完成时推 progress notice）

### 1.4 与 system-reminder 的关系

- **不替代 system-reminder**：reminder 是给模型的实时上下文（todo/plan/...），必须入库让历史携带；notice 是给用户的过程提示，不入库
- **不替代 WS 侧面板帧**：todo/taskflow progress 仍走专用面板帧（结构化、持续更新）；notice 是 chat 流里的一次性提示
- 三者并存：reminder（入库+模型+用户卡片）/ WS 侧面板帧（不入库+侧面板）/ system.notice（不入库+chat 流临时卡片）

---

## 2. 变更清单

| 文件 | 变更 |
|---|---|
| `client/app/composables/use-system-notice.ts` | **新建**：临时 notice 列表 + WS 帧处理 |
| `client/app/pages/home/components/ChatBox.vue` | 渲染 notice 临时卡片（level 颜色 + source + 折叠） |
| `client/app/composables/use-chat-stream.ts`（或 WS composable） | 处理 `system.notice` 帧类型 → push 进 notice 列表 |
| `server/service/` 新增 `system_notice_service.py` | `push_system_notice(session_id, notice)` → WS 广播 + 可选 HTTP 端点 |
| `server/trigger/http/` 新增 `POST /sessions/:sid/notice`（可选） | 外部 HTTP 推送入口 |

**不改**：MesMemory / checkpoint / transcript / DynamicReminderMiddleware / 现有 WS 侧面板帧。

---

## 3. 测试

- WS 推 `system.notice` → 前端 chat 流出现临时卡片 → 刷新后消失（不入库）
- MesMemory messages 表不含 notice 行
- history API 不含 notice
- 多 level 颜色正确；长内容折叠
- HTTP `POST /sessions/:sid/notice` → session 所有 socket 收到帧

---

## 4. Kill criteria

- 若 notice 临时卡片在 chat 流的位置定位复杂（虚拟滚动/turn 分组干扰），降级为 chat 顶部 toast（不进 chat 流，只浮层显示）

---

## 5. 备注

- 实现成本极低（一个 WS type + 前端列表 + 渲染分支）
- 不碰持久化链路，零回归风险
- 填的缺口：外部工具/CI/插件反馈进 chat 流不污染历史
