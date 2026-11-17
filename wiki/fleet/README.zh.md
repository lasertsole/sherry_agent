# 🏢 裸金属 Fleet：每租户一个 Sherry 进程

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> Fleet 部署为每个租户运行一个正常的 Sherry 后端，并在前面放一个 Ingress。每个 cell 拥有独立的 `SHERRY_HOME`、workspace 和 SQLite 存储；Ingress 负责浏览器登录，只转发已认证的流量。不需要 Docker，cell 也从不监听公网接口。

**事实来源：** `fleet/`、`packaging/MULTI_TENANT.md`、`tests/fleet/`。

## 🎯 它是什么

部署分为三层。浏览器只访问 Ingress（`python -m fleet serve`，默认端口 8081）。Ingress 认证用户，从 JWT claims 解析租户，再把 HTTP 与 WebSocket 流量反代到该租户的 cell。cell 是一个普通的 `python -m server` 进程，绑定 `127.0.0.1` 上 20100-20299 之间的端口，由 Fleet manager 以 detached 方式启动，并携带该 cell 专属的 gateway token。租户状态集中在一棵目录树里：`SHERRY_HOME`、`workspace/`、`sherry.jsonc` 和 `logs/`。

## 🏗️ 拓扑

```text
browser ──HTTP(S)──▶ Ingress :8081 ──loopback──▶ cell A  python -m server :20100
                             │                      SHERRY_HOME=<fleet_root>/a
                             ├──loopback──▶ cell B  python -m server :20101
                             │                      SHERRY_HOME=<fleet_root>/b
                             └──loopback──▶ shared daemons :9012 :9013 :9014 :9015
```

Ingress 与所有 cell 使用同一个 OS 用户。这是可信租户层级：边界是进程状态、端口和路由，而不是内核沙箱。

## 🚪 Ingress：认证与浏览器契约

Ingress 拥有所有 `/auth/*` 路由，并且从不代理 `/auth/token`，因此 cell 每次启动生成的 gateway token 不会泄漏给浏览器。登录会设置两个 HttpOnly cookie（`sherry_ingress_access`、`sherry_ingress_refresh`）。access token 是短期 HS256 JWT，携带 `tenants`（默认值在前）、`tenant_id` 和 `role`；refresh token 每次使用都会轮换，并可在服务端撤销。

共享 SPA 看到的契约与单租户 cell 一致。`/auth/status` 额外返回 `username_required`；`/auth/login` 接受 `{username, password}` 并返回 `expires_in`；`/auth/me` 可用有效的 refresh cookie 治愈过期的 access cookie；`/auth/account` 用于修改密码，启用/禁用登录仍只允许管理员操作。非安全方法请求会经过 Origin 与 `Sec-Fetch-Site` 的 CSRF 检查。同一 `(client, username)` 在 300 秒内失败 5 次会返回 `429`。

设置 `SHERRY_INGRESS_TLS_CERT` 和 `SHERRY_INGRESS_TLS_KEY` 后，Ingress 自行终止 TLS，并把两个 cookie 标记为 `Secure`；`SHERRY_INGRESS_ALLOWED_ORIGINS` 可追加反向代理的 origin。静态资源公开，任何带 `Accept: text/html` 的 GET 都会收到 `200.html` 或 `index.html`，所以 `/login` 与深层路由在会话建立前即可加载。API 调用仍然必须认证。

## 👥 用户、信任域与 Cell 切换

Cell 是信任域，不是用户账号：任意多个账号都可以指向同一个 `tenant_id`，从而共享该 cell 的会话、记忆、技能和项目目录。凭据仍按账号保存在 Ingress 数据库中，修改一个账号的密码不会影响另一个。一个账号也可以属于多个 cell，关系保存在多对多的 `user_tenants` 表中（旧的 `users.tenant_id` 继续作为默认值）。`init()` 会一次性重建 `tenant_id` 曾为 `UNIQUE` 的旧 `users` 表，并回填成员关系。

登录时选择账号的默认成员关系，并在认证 cookie 之外设置 HttpOnly 的 `sherry_tenant` cookie。`GET /auth/tenants` 返回每个成员关系的 `role`、`is_default` 和 `cell_status`；`POST /auth/tenant` 用实时表校验请求的 `tenant_id` 并重写 cookie。代理与 WebSocket 路由在每次请求时读取该 cookie 并重新校验成员关系，因此管理员撤销某个 cell 会立即生效；失效的 cookie 会回退到账号默认值。SPA 的 `TenantSwitcher` 只在账号拥有多个 cell 时出现，切换后刷新页面，因为会话缓存、记忆和 socket 都属于上一个 cell。

没有任何成员关系的账号无法登录（`no_tenant`）。使用 `user-tenant-add`、`user-tenants` 和 `user-tenant-remove` 管理访问权限；第一个成员关系是 owner 和默认值。`rm` 会删除 cell 但保留成员关系，请先撤销或重新分配。

## 🧩 Cell 生命周期

`python -m fleet create --tenant alice` 会创建 `<fleet_root>/alice/{workspace,logs,sherry.jsonc}`，并分配 20100-20299 中第一个空闲端口。端口选择与注册表写入由进程内锁加 fleet 根目录的 `flock` 串行化，因此两个 CLI 进程不会注册同一个端口。`start` 注入 `SHERRY_HOME`、`SHERRY_PROJECT_DIR`、`API_PORT`、`SHERRY_GATEWAY_TOKEN`、`PUBLIC_BASE_URL`、`SHERRY_DAEMON_HOME` 和 `SHERRY_DAEMON_CLIENT_ONLY`，以 detached 方式启动服务器，并轮询 `/health` 直到预算耗尽。

`stop` 只会向环境变量中仍指向该 cell `SHERRY_HOME` 的 PID 发信号；被复用的或外来的 PID 不会被误杀。`create` 还支持 `--seed-config`，用于复制宿主机的 `.models` 和 `.network` 文件（默认关闭，因为这些文件包含凭据）。

## 🛡️ 监督与资源限制

| 症状 | Supervisor 的响应 |
| :--- | :--- |
| 记录的 PID 已消失，或其 `SHERRY_HOME` 不再匹配 | 回收陈旧子进程并启动替代进程 |
| 进程仍活着，但 `/health` 连续三次失败 | 先 `stop`，再 `start` |
| 已停止的 cell 留下未回收的 zombie | 在替代进程绑定端口前用 `waitpid` 回收 |
| `logs/cell.log` 超过 `SHERRY_FLEET_LOG_MAX_BYTES` | copy-truncate 到 `cell.log.1`，对运行中进程的 O_APPEND 描述符安全 |
| cell 超过 `SHERRY_FLEET_CELL_MEMORY_MB` 或 `SHERRY_FLEET_CELL_NOFILE` | exec 包装层已施加 `ulimit`；CPU 配额仍由 cgroup 或 systemd 负责 |

Supervisor 运行在 Ingress 内，每隔 `SHERRY_FLEET_SUPERVISE_INTERVAL_S` 秒探测每个 running cell。`SHERRY_FLEET_SUPERVISE=0` 可关闭该循环，交给外部运维管理。

## 💾 备份与恢复

`python -m fleet backup --tenant alice --out <dir>` 复制 cell 目录树，跳过 `logs/`、`-wal` 和 `-shm`，并对每个 `.db` 使用 `sqlite3.Connection.backup()` 生成快照，因此 SQLite 文件在事务层面一致。cell 运行期间，非数据库文件是 best-effort。

`restore` 先停止 cell，把备份写入 staging，将当前目录树改名为 `<tenant>.pre-restore`，再换入 staging。`python -m fleet backup-db --out <dir>` 备份中央 `fleet.db`（用户、refresh token、cell 与进程行）。

## 🤝 共享模型 daemon

`python -m fleet daemons-start` 在 `<fleet_root>/daemons` 下为每种本地模型 daemon（embed、reranker、local chat、vision）维护唯一一份进程。cell 以 `SHERRY_DAEMON_CLIENT_ONLY=1` 运行：它们连接已记录的 pid，绝不启动第二份，也绝不注册 atexit kill。`SHERRY_DAEMON_HOME` 让每个 cell 指向共享的 pid 目录。

Local chat 与 vision 还需要 `SHERRY_FLEET_AUXILIARY_MODEL_PATH`、`SHERRY_FLEET_VISION_MODEL_PATH` 和 `SHERRY_FLEET_VISION_MMPROJ_PATH`。缺少这些变量时，命令会报告缺失项，而不是启动一个永远无法加载的 daemon。daemon 不在线时，客户端会退回进程内加载，功能仍然可用，但每个 cell 的内存不再共享。

## 🖥️ CLI

```bash
python -m fleet serve
python -m fleet user-add --username alice --password '...' --tenant alice
python -m fleet user-tenant-add --username alice --tenant team-b --role member
python -m fleet user-tenants --username alice
python -m fleet user-tenant-remove --username alice --tenant team-b
python -m fleet create --tenant alice
python -m fleet start --tenant alice
python -m fleet stop --tenant alice
python -m fleet list
python -m fleet backup --tenant alice --out /srv/backups/alice
python -m fleet restore --tenant alice --from /srv/backups/alice
python -m fleet backup-db --out /srv/backups/fleet
python -m fleet daemons-start
python -m fleet daemons-stop
python -m fleet daemons-status
```

## 🔐 安全边界

| 边界 | 部署提供什么保证 |
| :--- | :--- |
| 浏览器凭据 | HttpOnly、SameSite=Lax cookie；`/auth/token` 在 Ingress 上是 404 |
| Cell 凭据 | 每个 cell 一个 `SHERRY_GATEWAY_TOKEN`；浏览器自带的 token 与 ticket 会被剥离 |
| Cell 网络 | cell 只绑定 `127.0.0.1`；Ingress 通过 loopback 访问 |
| 租户文件 | `SHERRY_HOME` 隔离数据树，但所有 cell 共享一个 OS 用户；恶意租户需要 P3 容器层级 |

- 同一 tenant 的账号共享该 cell 的会话和记忆；按用户隔离需要拆分为不同 tenant。

## 🧪 测试

`tests/fleet/` 覆盖存储、Ingress 认证、代理、WebSocket 与 TLS、manager 生命周期、共享 daemon 以及备份/恢复。`test_cell_e2e.py` 通过 manager 启动真实的 `python -m server` cell，验证 `/health`、PID 身份、登录、HTTP 代理和干净的 SIGTERM 退出。

`pytest tests/fleet` 与仓库的 split gate 是回归命令。客户端认证有独立的 store、登录页、路由守卫和账户面板测试套件；后端全量 gate 会在独立进程中分别运行 unit、integration 和 regression 组。

## 📌 运维要求

- 用 systemd 或 supervisord 守护 Ingress：它内置的 supervisor 只管理 cell，不管理自身。
- 以同源方式构建 SPA：`VITE_API_BACK_URL=same-origin`，并把 `PUBLIC_BASE_URL` 设为浏览器可达的 Ingress origin。
- 设置稳定的 `SHERRY_INGRESS_JWT_SECRET`；每次启动随机生成会导致重启后所有 cookie 失效。
- 在 Ingress 或可信反向代理处终止 TLS，并把额外 origin 写入 `SHERRY_INGRESS_ALLOWED_ORIGINS`。
- 为 manager 不负责的限制补充 cgroup 或 systemd CPU 配额，以及外部备份或 logrotate 定时任务。
- 为每个租户绑定项目目录；未绑定时两个租户会共享 `ROOT_DIR/.sherry/coordination.db`。
- 确保每个账号至少有一个成员关系；没有成员关系的登录会返回 `no_tenant`。
