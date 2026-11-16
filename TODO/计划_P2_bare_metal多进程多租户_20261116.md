# 计划 P2：Bare-metal 多进程多租户——无 Docker，每租户独立进程

- 日期：2026-11-16
- 状态：**待评审**
- 定位：**第一级多租户**。每租户一个独立 Python 进程，`SHERRY_HOME` 隔离数据，Ingress 统一认证 + 路由。不需 Docker，Windows/Linux/macOS 均可。
- 风险等级：**中** — Sherry 本体 ~30 行改动 + 新增 Ingress (~800 行) + Fleet process manager (~600 行)
- 依赖：P1（daemon host env override + LDW journal 路径修复）
- 可独立执行：✅ 不依赖 P3（P3 在此基础上加容器化）

---

## 0. 架构

```
┌──────────────────── Host (无 Docker) ────────────────────┐
│                                                            │
│  ┌─── Process: user-A ────┐  ┌─── Process: user-B ────┐  │
│  │  SHERRY_HOME=            │  │  SHERRY_HOME=            │
│  │    /data/cells/userA     │  │    /data/cells/userB     │
│  │  SHERRY_PROJECT_DIR=     │  │  SHERRY_PROJECT_DIR=     │
│  │    /data/cells/userA/ws  │  │    /data/cells/userB/ws  │
│  │  API_HOST=127.0.0.1      │  │  API_HOST=127.0.0.1      │
│  │  API_PORT=20100          │  │  API_PORT=20101          │
│  │  SHERRY_GATEWAY_TOKEN=   │  │  SHERRY_GATEWAY_TOKEN=   │
│  │    <tokenA>              │  │    <tokenB>              │
│  │  PUBLIC_BASE_URL=        │  │  PUBLIC_BASE_URL=        │
│  │    https://sherry.ex.com │  │    https://sherry.ex.com │
│  │                          │  │                          │
│  │  Sherry :20100           │  │  Sherry :20101           │
│  │  独立 12 DB / workspace  │  │  独立 12 DB / workspace  │
│  │  独立 persona / memory   │  │  独立 persona / memory   │
│  └──────────────────────────┘  └──────────────────────────┘ │
│                                                            │
│  ┌─── 本地模型 daemon（共享）──────────────────────┐       │
│  │  LOCAL_DAEMON_HOST=0.0.0.0                        │       │
│  │  Embed :9012 / Reranker :9013 / LLM :9014        │       │
│  │  所有 cell 连 127.0.0.1:<port>（同进程空间）      │       │
│  └───────────────────────────────────────────────────┘      │
│                                                            │
│  ┌─── Ingress Gateway ──────────────────────────────┐      │
│  │  JWT 多账户认证                                    │      │
│  │  路由: JWT tenant_id → 127.0.0.1:<port>           │      │
│  │  HTTP + WS 反代                                   │      │
│  │  共享前端静态文件                                  │      │
│  └───────────────────────────────────────────────────┘      │
│                                                            │
│  sherry-fleet --backend process 管理进程                   │
│  supervisord (Linux) / Windows Scheduled Task              │
└────────────────────────────────────────────────────────────┘
```

---

## 1. 隔离分析

| 维度 | 隔离？ | 机制 |
|------|--------|------|
| 进程 | ✅ | 独立 Python 进程 |
| 12 SQLite DB | ✅ | `SHERRY_HOME` → `APP_HOME/src/data/` per-user |
| workspace / memory / skills / persona | ✅ | `SHERRY_HOME` → `APP_HOME/workspace/` per-user |
| 配置文件 | ✅ | `SHERRY_HOME` → `APP_HOME/sherry.jsonc` per-user |
| Lane（并发预算） | ✅ | 进程级单例，per-process 独立 |
| 本地模型 daemon | ✅ 共享 | host 上一份，所有进程连 `127.0.0.1:<port>` |
| **文件系统权限** | ❌ | 同一 OS 用户，进程有权限读对方目录 |
| **网络** | ❌ | 共享网络栈，只是不同端口 |
| **terminal/python_repl** | ❌ | agent 工具直接在 host 上跑，能读对方文件 |

**定位：可信租户（企业内部团队）。** 不可信租户需 P3（容器隔离）。

---

## 2. Sherry 本体改动（~30 行）

### 2.1 Cell auth 禁用（bare-metal 模式）

**问题**：Ingress 反代到 Sherry 进程时，`user_auth_middleware`（`auth_user.py:115`）要求非 loopback peer 有 session cookie。

**bare-metal 模式**：Ingress 和 Sherry 进程在同一 host，Ingress 连 `127.0.0.1:<port>` → `is_loopback` = True → `require_auth_non_loopback`（默认 True）→ **loopback 豁免，auth 自动 pass-through**。

**但 `SHERRY_AUTH_ENABLED=false` 不够**：`AUTH["enabled"]` 是 first-boot seed（`auth_store.seed_auth_enabled_once`），只在新 DB 无 `auth_settings` 行时生效。已有 DB 行不受 env 影响。runtime enforcement 是 `auth_service.enforcement_active()`，读 SQLite `auth_settings.enabled`，不读 env。

**实际方案**（bare-metal 模式）：
- loopback 连接 → `is_loopback=True` → `require_auth_non_loopback` 豁免 → **不需禁用 auth，不需要改代码，不需要设 env**
- Ingress 绑定 `127.0.0.1`，连 cell 的 `127.0.0.1:<port>` → cell 看到 loopback → pass-through

**如果 Ingress 绑定非 loopback**（如 `0.0.0.0`）：
- cell 看到 Ingress 的非 loopback IP → auth 拦截
- 需通过 `auth_store.set_auth_enabled(False)` 在 DB 层禁用（Fleet 创建 cell 后调一次），或保持 Ingress loopback

**推荐**：Ingress 绑定 loopback，cell 也绑定 loopback，auth 自动豁免。不设任何 auth env。

### 2.2 `PUBLIC_BASE_URL` env（~20 行）

**问题**：6 处 URL 构造用 `f"http://{API_HOST}:{API_PORT}/..."` 返回客户端。bare-metal 模式 `API_HOST=127.0.0.1` → URL 含 `127.0.0.1` → 客户端不可达。

**改动**：

`config/__init__.py` 新增（在 `API_HOST`/`API_PORT` 旁）：
```python
PUBLIC_BASE_URL: str = os.environ.get("PUBLIC_BASE_URL", f"http://{API_HOST}:{API_PORT}")
```

改以下 6 处（精确文件:行号）：

| 文件:行号 | 当前代码 | 改为 |
|-----------|---------|------|
| `server/trigger/http/image.py:88` | `f"http://{API_HOST}:{API_PORT}/images/{filename}"` | `f"{PUBLIC_BASE_URL}/images/{filename}"` |
| `server/trigger/http/video.py:66` | `f"http://{API_HOST}:{API_PORT}/video/{filename}"` | `f"{PUBLIC_BASE_URL}/video/{filename}"` |
| `server/trigger/http/audio.py:77` | `f"http://{API_HOST}:{API_PORT}/audio/{filename}"` | `f"{PUBLIC_BASE_URL}/audio/{filename}"` |
| `server/trigger/http/upload_chunked.py:128` | `f"http://{API_HOST}:{API_PORT}/{_KIND_DIR[kind]}/{filename}"` | `f"{PUBLIC_BASE_URL}/{_KIND_DIR[kind]}/{filename}"` |
| `server/trigger/http/channels.py:62` | `f"http://{API_HOST}:{API_PORT}/channels/{name}/icon/{candidate.name}"` | `f"{PUBLIC_BASE_URL}/channels/{name}/icon/{candidate.name}"` |
| `server/trigger/http/channels.py:70,76` | 同上 | 同上 |

每处 import 加 `PUBLIC_BASE_URL`（从 `config` import）。`PUBLIC_BASE_URL` 未设时 fallback 为 `f"http://{API_HOST}:{API_PORT}"` → 行为不变。

### 2.3 Gateway token 透传

Ingress 连 Sherry 进程时需带 Gateway token。`SHERRY_GATEWAY_TOKEN` env 在 import 时读取（`auth.py:70`），per-process 可配。Ingress 从 Fleet 注册表读取 per-user token，附加到每个请求（`?token=` query param 或 `token` header）。

### 2.4 API_PORT per-process

`API_PORT` env（无 `SHERRY_` 前缀）在 import 时读取（`gateway.py:62`），冻结到 `config.API_PORT`。per-process 可配——每个 Python 解释器有自己的 env。Fleet 为每进程设 `API_PORT=<分配端口>`。

### 2.5 /health 端点——liveness 足够，readiness 需扩展

现有 `/health`（`health.py:85`）返回 `ok: True`（硬编码），只验证 HTTP server 在监听，**不验证 agent core 初始化**。

Fleet 健康检查分两阶段：
- **liveness**（容器/进程是否存活）：用现有 `/health`，`ok:true` 足够
- **readiness**（agent core 是否就绪）：现有 `/health` 不够。`models_configured` 只检查 env 非空，不检查模型可达

**方案**：Fleet 创建后轮询 `/health` 60s 确认 liveness（进程起来了）。readiness 不阻塞——agent core 在后台初始化，第一个 user message 触发 `init_agent_core()`。如果需要 readiness probe，扩展 `/health` 加 `agent_ready: bool` 字段（~5 行，检查 `_agent` 是否初始化）。暂不做，liveness 足够。

---

## 3. Ingress Gateway（新增 ~800 行）

### 3.1 职责

1. 多账户 JWT 认证（复用 Sherry 的 `jwt_utils.py` + `password.py`）
2. 路由请求到对应租户的 Sherry 进程（`127.0.0.1:<port>`）
3. HTTP + WS 反代
4. 共享前端静态文件

### 3.2 用户账户

```sql
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    tenant_id TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL
);
```

### 3.3 认证流程

1. `POST /auth/login`（username + password）→ JWT `{user_id, username, tenant_id, role}`
2. JWT 放 HttpOnly cookie
3. HTTP 请求：解析 JWT → 取 `tenant_id` → 查 Fleet 注册表得 port → 反代到 `127.0.0.1:<port>`
4. WS 连接：同上 + Gateway token 透传

### 3.4 WS 反代

```python
async def ws_proxy(client_ws, target_port, gateway_token, path, query):
    url = f"ws://127.0.0.1:{target_port}{path}?token={gateway_token}&{query}"
    async with websockets.connect(url) as cell_ws:
        await asyncio.gather(
            forward(client_ws, cell_ws),
            forward(cell_ws, client_ws)
        )
```

### 3.5 前端改动（~100 行）

- 登录页加 username 输入
- auth store `user.id` 不再固定为 1
- session 创建时不自己生成 UUID（改为调 Ingress 的 `POST /sessions`）

---

## 4. Fleet Process Manager（新增 ~600 行）

### 4.1 职责

- 创建/列出/停止/删除 per-user Sherry 进程
- 端口分配（20100+）
- 目录准备（`/data/cells/<user>/` + workspace 子目录）
- per-user env 注入
- 进程健康检查（`/health`）
- 进程生命周期管理

### 4.2 fleet_cells 表

```sql
CREATE TABLE fleet_cells (
    tenant_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    backend TEXT NOT NULL DEFAULT 'process',
    host_port INTEGER NOT NULL,
    data_dir TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'stopped',
    gateway_token TEXT NOT NULL
);
```

### 4.3 CLI

```bash
sherry-fleet create --user alice --backend process
sherry-fleet list
sherry-fleet stop --user alice
sherry-fleet start --user alice
sherry-fleet rm --user alice
sherry-fleet backup --user alice   # 用 sqlite3.Connection.backup()
sherry-fleet restore --user alice --backup <path>
```

### 4.4 进程管理

| 平台 | 机制 |
|------|------|
| Linux | systemd user unit `sherry-cell-<tenant>.service` |
| macOS | launchd `ai.sherry.<tenant>` |
| Windows | Scheduled Task `Sherry Cell (<tenant>)` |

### 4.5 创建流程

1. 生成 tenant_id + gateway_token
2. 分配端口（20100+ 扫描）
3. 创建 `/data/cells/<tenant>/` + `/data/cells/<tenant>/workspace/`
4. 生成 per-user `sherry.jsonc`（设 `project_dir`）
5. 注入 env：`SHERRY_HOME` / `SHERRY_PROJECT_DIR` / `API_HOST` / `API_PORT` / `SHERRY_GATEWAY_TOKEN` / `SHERRY_AUTH_REQUIRE_NON_LOOPBACK=false` / `PUBLIC_BASE_URL`
6. 启动进程（systemd/launchd/Scheduled Task）
7. 健康检查（轮询 `/health` 60s）

---

## 5. 本地模型 daemon 共享

bare-metal 模式下 daemon 和 Sherry 进程在同一 host → daemon bind `127.0.0.1` → Sherry 进程连 `127.0.0.1:<port>` → **天然可达，不需改 host**。

P1 已加 `LOCAL_DAEMON_HOST` env override。bare-metal 模式下不需要设它（默认 `127.0.0.1` 就对）。

如果 daemon 要被 P3 的容器 cell 访问，才需设 `LOCAL_DAEMON_HOST=0.0.0.0`。

---

## 6. SQLite backup

`sherry-fleet backup` 用 `sqlite3.Connection.backup()` API（在线一致性备份），不裸文件复制。每 DB（最多 12 个）单独 backup。

---

## 7. 执行计划

| 阶段 | 内容 | 时间 |
|------|------|------|
| 1 | Sherry 本体改动（PUBLIC_BASE_URL + auth env） | ~2 天 |
| 2 | Ingress Gateway（JWT + HTTP/WS 反代 + 前端） | ~1 周 |
| 3 | Fleet process manager（CLI + 进程管理 + 健康检查） | ~4 天 |
| 4 | backup/restore（sqlite3 backup API） | ~2 天 |
| 5 | 测试 + 文档 | ~3 天 |

**总计：~3 周**

---

## 8. 风险

| 风险 | 严重性 | 缓解 |
|------|--------|------|
| terminal 可读对方文件 | High | 仅部署给可信租户（团队内部）。不可信租户用 P3 |
| 一个进程 OOM 影响 host | Medium | OS 级 ulimit / cgroup 限制（手动配） |
| WS 反代延迟 | Low | loopback 反代 <1ms |
| 无全局资源限制 | Medium | Fleet 根据 host 资源决定 cell 数量 |

---

## 9. Kill criteria

- 若 WS 反代延迟过高（>100ms），改为 302 重定向（客户端直连 cell + token 认证）
- 若 supervisord/systemd 管理多进程不稳定，降级为单进程 + 应用级多租户（回退到 r1 计划）
