# 计划 P2：Bare-metal 多进程多租户——无 Docker，每租户独立进程

- 日期：2026-11-16
- 状态：**P2 收尾完成（2026-11-17）：Phase 1–3b + 生产项（TLS/限流/SPA/真实 cell e2e）——剩余为 P3/运维层（§11）**
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

> **WS 例外**：`ws_user_check` 不检查 loopback。只要 cell 的 auth enforcement 生效（有账号且开关打开），WS 就需要一次 `ticket`。Ingress 必须通过 loopback `GET /auth/ws-ticket` 取票并附加到 WS URL，或在 Fleet 创建 cell 时于 DB 层关闭 enforcement；只带 gateway token 不够。

### 2.2 `PUBLIC_BASE_URL` env（~20 行）

**问题**：7 处 URL 构造用 `f"http://{API_HOST}:{API_PORT}/..."` 返回客户端。bare-metal 模式 `API_HOST=127.0.0.1` → URL 含 `127.0.0.1` → 客户端不可达。

**改动**：

`config/__init__.py` 新增（在 `API_HOST`/`API_PORT` 旁）：
```python
PUBLIC_BASE_URL: str = os.environ.get("PUBLIC_BASE_URL", f"http://{API_HOST}:{API_PORT}")
```

改以下 7 处（精确文件:行号；channels.py 有 3 行）：

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

Ingress 连 Sherry 进程时需带 Gateway token。`SHERRY_GATEWAY_TOKEN` env 在 import 时读取（`auth.py:70`），per-process 可配。Ingress 从 Fleet 注册表读取 per-user token，附加到每个请求（`?token=` query param 或 `token` header）。Ingress 必须自己持有 `/auth/*`（尤其 `/auth/token` 是公开 bootstrap 路径，会返回 cell 的 gateway token），不得把该前缀透传给 cell。

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

> 这段只示意转发。生产实现还要：cell auth enforcement 开启时 mint `ticket` 并附加；转发 subprotocol；处理 close 码、ping/pong、背压与取消；WS upgrade 的 Origin/路由必须在 Ingress 侧验证。

### 3.5 前端改动（spike 后修订）

- 登录页加 username 输入；`/auth/*` 请求全部由 Ingress 处理，不转发给 cell。
- Ingress 持有中央 `users` 表、JWT 和 cookie；cell 的 auth store 保持单账号，不需要改成多用户。
- 会话 UUID 继续由前端生成；不需要 Ingress `POST /sessions`——JWT 已确定 tenant，cell 自己拥有 sessions。
- **构建期**设置 `VITE_API_BACK_URL` 为公网 Ingress 地址（或把客户端改成相对 base）；`WS_BASE_URL` 由它派生。当前默认是 `http://localhost:8080`，不设置时远程浏览器会请求用户自己的 localhost。

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
| 默认 | `sherry-fleet` supervisor：pid/日志/健康检查，复用 `models/daemon` 的 detached spawn 与 kill 语义 |
| Linux（可选） | systemd user unit `sherry-cell-<tenant>.service` |
| macOS（可选） | launchd `ai.sherry.<tenant>` |
| Windows（可选） | Scheduled Task `Sherry Cell (<tenant>)` |

跨平台 unit/任务生成不是第一版必需项；先把 supervisor 做稳，systemd/launchd 作为包装层。

### 4.5 创建流程

1. 生成 tenant_id + gateway_token
2. 分配端口（20100+ 扫描）
3. 创建 `/data/cells/<tenant>/` + `/data/cells/<tenant>/workspace/`
4. 生成 per-user `sherry.jsonc`（设 `project_dir`）
5. 注入 env：`SHERRY_HOME` / `SHERRY_PROJECT_DIR` / `API_HOST` / `API_PORT` / `SHERRY_GATEWAY_TOKEN` / `SHERRY_ALLOWED_ORIGINS=<client-facing origin>` / `PUBLIC_BASE_URL`；cell auth 建议在 DB 层关闭 enforcement（`set_auth_enabled(False)`），或由 Ingress 每连接 mint ws ticket；`SHERRY_AUTH_REQUIRE_NON_LOOPBACK=false` 只影响 HTTP，不解决 WS。
6. 启动进程（systemd/launchd/Scheduled Task）
7. 健康检查（轮询 `/health` 60s）

---

## 5. 本地模型 daemon 共享

bare-metal 模式下 daemon 和 Sherry 进程在同一 host → daemon bind `127.0.0.1` → Sherry 进程连 `127.0.0.1:<port>` → **天然可达，不需改 host**。但 daemon 的 pid 文件在各自 cell 的 `APP_HOME` 下：每个 cell 都可能尝试 spawn 同端口 daemon，且 spawn 方会注册 atexit kill。**Fleet 必须统一持有共享 daemon**（固定 host home/pid、启动与健康检查），否则一个 cell 退出可能杀掉所有 cell 的 daemon。

P1 已加 `LOCAL_DAEMON_HOST` env override。bare-metal 模式下不需要设它（默认 `127.0.0.1` 就对）。

如果 daemon 要被 P3 的容器 cell 访问，才需设 `LOCAL_DAEMON_HOST=0.0.0.0`。

---

## 6. SQLite backup

`sherry-fleet backup` 用 `sqlite3.Connection.backup()` API（在线一致性备份），不裸文件复制。每 DB（最多 12 个）单独 backup；`workspace/`（sessions、媒体、uploads、persona）另行打包，restore 时按同一时间点恢复，避免 DB 与文件不一致。

---

## 7. 执行计划

| 阶段 | 内容 | 时间 |
|------|------|------|
| 1 | Sherry 本体改动（PUBLIC_BASE_URL + auth env） | ~2 天 |
| 2 | Ingress Gateway（JWT + HTTP/WS 反代 + 前端） | ~1 周 |
| 3 | Fleet process manager（CLI + 进程管理 + 健康检查） | ~4 天 |
| 4 | backup/restore（sqlite3 backup API） | ~2 天 |
| 5 | 测试 + 文档 | ~3 天 |

**总计：4–6 周**（spike 复核后修订；原 3 周只覆盖了 happy path）

---

## 8. 风险

| 风险 | 严重性 | 缓解 |
|------|--------|------|
| terminal 可读对方文件 | High | 仅部署给可信租户（团队内部）。不可信租户用 P3 |
| 一个进程 OOM 影响 host | Medium | OS 级 ulimit / cgroup 限制（手动配） |
| WS 反代延迟 | Low | loopback 反代 <1ms |
| 无全局资源限制 | Medium | `SHERRY_FLEET_CELL_MEMORY_MB`/`NOFILE` 已由 Fleet 施加 ulimit；CPU 配额用 cgroup/systemd；cell 数仍按 host 容量 |
| Ingress 单点 / TLS | High | TLS、登录限流、中央 DB backup、cell supervisor 已实现；Ingress 进程本身仍需 systemd/supervisord 外部守护 |
| 每 cell 模型/网络配置 | High | Fleet 需 provision `.models`/`.network`（或集中代理）；模板 seed 不含真实 key |
| WS/auth 集成 | High | WS ticket 或 DB 关闭 enforcement 必须二选一并测试；不能只透传 gateway token |
| 客户端 base URL | High | 构建期地址必须是客户端可达的 Ingress；相对 base 需要客户端改动与测试 |

---

## 9. Kill criteria

- 若 WS 反代延迟过高（>100ms），改为 302 重定向（客户端直连 cell + token 认证）
- 若 supervisord/systemd 管理多进程不稳定，降级为单进程 + 应用级多租户（回退到 r1 计划）

## 10. Spike 结果（2026-11-16）

### 已验证

- `SHERRY_HOME` → `APP_HOME/src`，所有可写 DB、workspace、skills、logs 随 home 移动。
- `API_PORT` / `API_HOST` 从进程 env 读取，每租户独立进程可行。
- Ingress → cell 的 HTTP 走 loopback，`require_auth_non_loopback` 豁免成立；gateway token 可按进程配置。
- project_dir、LDW journal、daemon host 的 P1 前置已落地。

### 必须补的集成缺口

| # | 缺口 | 影响 / 修法 |
|---|---|---|
| 1 | 客户端 API/WS base 是构建期 `VITE_API_BACK_URL`，默认 `localhost:8080` | 远程浏览器连自己的 localhost；构建时设客户端可达的 Ingress 地址（内网 IP/DNS 或公网域名），或支持相对 base |
| 2 | cell WS 鉴权不受 HTTP loopback 豁免 | `ws_user_check` 在 auth enforcement 时需要 ticket；Ingress 通过 loopback `/auth/ws-ticket` 取 ticket，或 DB 关闭 cell auth enforcement |
| 3 | cell 默认 Origin 白名单只有 Tauri origin | 浏览器经 Ingress 带来的 Origin 会被 403；注入 `SHERRY_ALLOWED_ORIGINS`（内网或公网 origin），或 Ingress 剥离浏览器 Origin 并自己承担 CSRF |
| 4 | `/auth/token` 是公开路径且返回 cell gateway token | Ingress 必须接管/拒绝 `/auth/*`，至少显式阻断 `/auth/token` |
| 5 | 共享 daemon 没有 owner | Fleet 统一启动/持有；否则 per-cell pid 与 atexit kill 会互相踩 |
| 6 | 不能复用 cell 单账号登录 | `auth_store` 是 `id=1` 单行，JWT 只有 `user_id`；Ingress 自建 users 表与 `tenant_id/role` claim |
| 7 | cell 的模型/API 配置未 provision | `SHERRY_HOME` 只会 seed 模板；Fleet 需注入真实 `.models`/`.network` 或集中代理 |
| 8 | systemd/launchd/Scheduled Task 过重 | 复用 `models/daemon` 的 pid/spawn/health 模式，systemd 作为可选包装 |

### 修订后的工期

原计划 3 周偏乐观。生产级 P2 建议 **4–6 周**：

1. 客户端 base URL + `PUBLIC_BASE_URL` + runtime 配置（2–3 天）
2. Ingress：中央 users/JWT、cookie/refresh/ws ticket、HTTP/WS 反代、路由归属（约 2 周）
3. Fleet：进程 supervisor、共享 daemon、端口/健康/日志/backup（约 1 周）
4. Origin/CSRF、token 泄漏、TLS、限流、测试与文档（约 1 周）

### 内网测试

不需要公网。这里的地址只要**浏览器能访问到 Ingress**：

- 同机测试：`VITE_API_BACK_URL=http://127.0.0.1:<ingress-port>`；
- 局域网设备测试：`VITE_API_BACK_URL=http://<host-LAN-IP>:<ingress-port>`（或内网 DNS）；
- Ingress 需要被其他设备访问时才绑 `0.0.0.0`；cell 始终只绑 `127.0.0.1`；
- `SHERRY_ALLOWED_ORIGINS` 填同一个浏览器 origin；
- HTTP 内网测试时 cookie 不要设 `Secure`（HTTPS 再开）。

更稳的做法是让客户端支持相对 base（同源），这样同一份构建在 localhost、内网 IP 和公网域名下都能用，不需要为每个环境重新构建。

### 先做 2 天 spike

1. 构建指向客户端可达 Ingress（内网 IP/DNS 或公网域名）的 SPA，起 1 个 cell + 简易反代，验证 HTTP/WS/媒体 URL。
2. 选 cell auth 模式（DB 关闭 enforcement 或 mint ws ticket），实测 WS。
3. 配 `SHERRY_ALLOWED_ORIGINS`，验证 POST/PUT 不被 Origin/CSRF 拦截。
4. 验证 `/auth/token` 不被代理、token 不泄漏。
5. 起 2 个 cell + Fleet 持有的共享 daemon，验证隔离与 daemon 不被单 cell 杀掉。
6. 按结果重估 Ingress/Fleet 工期。

## 11. 执行进度

- [x] Phase 1a：`PUBLIC_BASE_URL` runtime 配置 + 7 处 URL 构造（2026-11-16；`resolve_public_base_url` + 测试，未设置时保持 `http://API_HOST:API_PORT`）
- [x] Phase 1b：客户端同源 base（`VITE_API_BACK_URL=same-origin`），避免按环境重建（2026-11-16；`env-base.test.ts`）
- [x] Phase 2：Ingress 中央 users/JWT、cookie/refresh/ws ticket、HTTP/WS 反代（2026-11-16；`fleet/` + fake-cell 集成测试）
- [x] Phase 3a：Fleet supervisor：create/start/stop/rm/list、per-cell 目录/env、健康检查（2026-11-17；`fleet/manager.py`）
- [x] Phase 3b：共享 daemon owner + backup/restore + 崩溃重启/端口锁/日志（2026-11-17）
- [x] Phase 4：Origin/CSRF、token 泄漏、TLS、限流、SPA 路由、真实 cell e2e（2026-11-17）

### Phase 1 落地记录

- 1a：`config/__init__.py::resolve_public_base_url()` 读取 `PUBLIC_BASE_URL`，去掉尾部 `/`；未设置时回退 `http://{API_HOST}:{API_PORT}`。`image/video/audio/upload_chunked/channels` 共 7 处 URL 改为使用它；`tests/config/test_public_base_url.py` 钉住覆盖与回退。
- 1b：`client/app/composables/env.ts` 接受哨兵值 `VITE_API_BACK_URL=same-origin`：REST 走相对路径，WS 从 `window.location.origin` 派生；默认仍是 `http://localhost:8080`。`env-base.test.ts` 覆盖两种模式。
- 下一步（Phase 2）：先做 Ingress 的最小鉴权骨架（中央 users 表、JWT claims、cookie/refresh、`/auth/token` 不透传），再接 HTTP 反代，最后接 WS + ticket。

### Phase 2 落地记录

- `fleet/config.py`：`SHERRY_INGRESS_*` 配置（host/port/db/JWT secret/cookie/origin/static）。
- `fleet/store.py`：中央 `users` / `refresh_tokens` / `cells` SQLite；scrypt 密码复用 `server/utils/password.py`。
- `fleet/security.py`：access/refresh JWT（tenant/role claims）、HttpOnly cookie、Origin/CSRF 检查。
- `fleet/ingress.py`：`/auth/login|refresh|logout|status`、`/health`、HTTP streaming 反代、WebSocket 反代（loopback 取 cell ws ticket）、`/auth/token` 显式 404（不泄漏 cell gateway token）、请求侧剥离 Cookie/Origin/Sec-Fetch-* 并注入 per-cell gateway token。
- `fleet/__main__.py`：`serve` / `user-add` / `cell-set` / `cells` 管理命令。
- 测试：`tests/fleet/test_store.py` + `tests/fleet/test_ingress.py`（fake cell；登录、代理、token 阻断、WS echo、CSRF 拒绝）。
- 仍缺：登录失败限流、TLS 终止、静态 SPA 的完整路由、cell auth enforcement 的正式选择（Ingress ticket vs DB 关闭）、Fleet supervisor 与共享 daemon owner。

### Phase 3a 落地记录

- `fleet/manager.py`：`create` 建 `SHERRY_HOME`/`workspace`/`sherry.jsonc`，分配 20100+ 端口，写 cell 注册表；`start` 注入 `SHERRY_HOME`/`SHERRY_PROJECT_DIR`/`API_PORT`/gateway token/`PUBLIC_BASE_URL`，Popen detached，轮询 `/health`；`stop` 先验证 PID 的 `SHERRY_HOME` 属于该 cell（防 PID 复用误杀），再 SIGTERM→SIGKILL；`remove` 删目录与注册表；`list` 合并进程状态。
- `--seed-config` 可选复制宿主 `.models`/`.network`（默认关闭，避免默认扩散密钥）。
- CLI：`python -m fleet create|start|stop|rm|list`。
- 测试：`tests/fleet/test_manager.py`（建树、env/PID、stop/remove、seed 配置）。
- Phase 3b 已完成：共享 daemon owner、backup/restore、崩溃重启/端口锁/日志/资源限制（见下）。

### Phase 3b 落地记录（2026-11-17）

- 共享 daemon：`config/path.py::DAEMON_HOME`（`SHERRY_DAEMON_HOME`）统一 pid 目录；`models/daemon/base.py` 支持 `SHERRY_DAEMON_CLIENT_ONLY`（cell 只连不 spawn、不注册 atexit）；embed/reranker/local-chat/vision 的 pid 全部落在共享 daemon home；`fleet/daemons.py` 是 host 侧唯一 owner，CLI `daemons-start|stop|status`。`FleetManager.start` 给 cell 注入共享 home + client-only。
- local-chat/vision：`SHERRY_LOCAL_LLM_DAEMON_PORT` 由 Fleet 传给子进程（否则两个 role 会抢 9014）；权重路径必须由 `SHERRY_FLEET_AUXILIARY_MODEL_PATH` / `SHERRY_FLEET_VISION_MODEL_PATH` + `SHERRY_FLEET_VISION_MMPROJ_PATH` 显式提供，缺失时 `daemons-start` 返回 `ready:false + error`，不会起一个永远 warming 的进程。
- backup/restore：`FleetManager.backup` 对每个 `.db` 用 `sqlite3.Connection.backup()`（在线一致），跳过 `logs/` 与 `-wal/-shm`；`restore` 先停、写 staging、旧树改名 `.pre-restore` 再换入，原进程在跑则重启。中央 `fleet.db` 另有 `python -m fleet backup-db`。
- 崩溃重启 / 端口锁 / 日志：Ingress 启动 `supervise_forever`，对被杀或 PID 身份不再匹配的 running cell 重启，并对活着但连续 3 次 `/health` 失败的 cell 先 stop 再 start；`_port_guard` 用进程内锁 + fleet-root `flock` 把“选端口→写注册表”串行化；日志 copy-truncate（运行中的 O_APPEND fd 安全），supervisor 每轮也检查；`_reap` 回收 zombie，避免重启累积。
- 资源限制：`SHERRY_FLEET_CELL_MEMORY_MB` / `SHERRY_FLEET_CELL_NOFILE` 通过 `/bin/sh -c 'ulimit …; exec "$@"'` 施加，exec 保持记录的 PID；CPU 配额仍建议 cgroup/systemd。

### Phase 4 落地记录（2026-11-17）

- 登录限流：每 `(client, username)` 300s 内 5 次失败 → 429，成功后清零。
- TLS：`SHERRY_INGRESS_TLS_CERT/KEY` 存在即用 `ssl.SSLContext` 终止并置 `Secure` cookie；`fleet.__main__.build_ssl_context` 可单测。
- 静态 SPA：`/`、`_nuxt/`、`_ipx/`、`assets/`、favicon/robots/payload/200/404/manifest 公开；任何 `Accept: text/html` 的 GET（含 `/login` 与 `/home/<sid>`）都回 `200.html`/`index.html` —— SPA shell 本身公开、不含数据，未登录也要能加载登录页；API 请求仍必须认证。浏览器自带的 `token`/`ticket` header 与 WS query 会被剥离，只转发 Ingress mint 的值。
- 前端契约：`/auth/status` 返回 `auth_required/auth_enabled/has_account/username_required`；login 接受 `{username,password}` 并回 `expires_in`；refresh 回 `user`；新增 `/auth/me`（access 失效时用 refresh 轮换治愈）与 `/auth/account`（改密；enable/disable 由管理员拒绝）；登录页在 `username_required` 时显示用户名。
- 真实 cell e2e：`tests/fleet/test_cell_e2e.py` 真起一个 detached cell 进程，验证 `/health`、PID 身份、Ingress 登录/HTTP/WS、SIGKILL 后 supervisor 重启、`stop`。
- 仍保留：cell 的 database-level auth 仍是 loopback 豁免（cell 只绑 127.0.0.1）；Ingress 进程本身仍需 systemd/supervisord 守护；不可信租户的文件隔离仍属 P3。
