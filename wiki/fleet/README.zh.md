# 🏢 Fleet 部署：每租户一个 Sherry cell

[**English**](README.md) · **中文** · [한국어](README.ko.md) · [日本語](README.ja.md)

> Fleet 部署为每个租户运行一个 Sherry 后端，并在前面放一个 Ingress。每个 cell 拥有独立的 `SHERRY_HOME`、workspace 和 SQLite 存储；Ingress 负责浏览器登录，只转发已认证的流量。同一套接口下有两种后端：面向可信租户的裸金属 **process** 层，以及面向不可信租户的 **container** 层（Docker 或 Podman），后者提供操作系统级的文件系统、网络与 cgroup 隔离。cell 从不监听公网接口。

**事实来源：** `fleet/`、`packaging/MULTI_TENANT.md`、`tests/fleet/`。

## 🎯 它是什么

部署分为三层。浏览器只访问 Ingress（`python -m fleet serve`，默认端口 8081）。Ingress 认证用户，从 JWT claims 解析租户，再把 HTTP 与 WebSocket 流量反代到该租户的 cell。cell 要么是一个普通的 `python -m server` 进程，要么是运行同一服务镜像的容器，绑定 `127.0.0.1` 上 20100-20299 之间的端口，由 Fleet manager 启动并携带该 cell 专属的 gateway token。租户状态集中在一棵目录树里——`SHERRY_HOME`、`workspace/`、`sherry.jsonc`、`logs/`——两种后端挂载或指向同一棵树，因此租户可以在两者之间迁移。

## 🏗️ 拓扑

```text
browser ──HTTP(S)──▶ Ingress :8081 ──loopback──▶ cell A  process  :20100
                             │                      SHERRY_HOME=<fleet_root>/a
                             ├──loopback──▶ cell B  container :20101
                             │                      /app/data ← <fleet_root>/b
                             └──loopback──▶ shared daemons :9012 :9013 :9014 :9015
```

Ingress 与所有 cell 使用同一个 OS 用户。process 层是可信租户层级：边界是进程状态、端口和路由，而不是内核沙箱。container 层额外提供独立的挂载命名空间、专属 bridge 网络，以及硬性的 CPU、内存和 pids 上限。

## 🚪 Ingress：认证与浏览器契约

Ingress 拥有所有 `/auth/*` 路由，并且从不代理 `/auth/token`，因此 cell 每次启动生成的 gateway token 不会泄漏给浏览器。登录会设置两个 HttpOnly cookie（`sherry_ingress_access`、`sherry_ingress_refresh`）。access token 是短期 HS256 JWT，携带 `tenants`（默认值在前）、`tenant_id` 和 `role`；refresh token 每次使用都会轮换，并可在服务端撤销。

共享 SPA 看到的契约与单租户 cell 一致，只有一处不同：Ingress 登录是「用户名 + 密码」（见下方多用户章节），因此 `/auth/status` 还会返回 `username_required: true`。`/auth/me` 可用有效的 refresh cookie 治愈过期的 access cookie；非安全方法请求会经过 Origin 与 `Sec-Fetch-Site` 的 CSRF 检查。

设置 `SHERRY_INGRESS_TLS_CERT` 与 `SHERRY_INGRESS_TLS_KEY` 后由 Ingress 终止 TLS，并把两个 cookie 标记为 `Secure`；`SHERRY_INGRESS_ALLOWED_ORIGINS` 追加反向代理来源。静态资源公开，任何 `Accept: text/html` 的 GET 都会拿到 `200.html` 或 `index.html`，所以 `/login` 与深层路由在会话建立前就能加载；API 调用仍然受限。

## 👥 多用户能力：账号、cell 与切换

Ingress 是唯一知道「人」的组件，cell 永远看不到用户名。账号由**管理员预置**（没有自助注册），存放在 Ingress 数据库（`users` 与多对多的 `user_tenants`）。SPA 在 Ingress 上永远显示登录页（`/auth/status` 返回 `auth_required: true`、`username_required: true`、`has_account: true`），`POST /auth/login` 接受 `{username, password}` 并返回 `expires_in`；失败尝试按 `(客户端地址, 用户名)` 限流：300 秒内 5 次返回 `429`，而未知用户名同样会做一次假哈希校验，避免用时间差探测账号是否存在。没有成员关系的账号会被拒绝为 `403 no_tenant`。

| 概念 | 存放位置 | 含义 |
| :--- | :--- | :--- |
| 账号 | Ingress `users` | 用户名 + scrypt 哈希——人唯一持有的凭据。修改密码是自助操作（`PUT /auth/account`）；启用/禁用登录与删除账号属于管理员操作，返回 `managed_by_admin`。 |
| `role` | `users.role` / `user_tenants.role` | 会被记录并返回（`GET /auth/tenants`、`/auth/me`），但**不做权限判定**：授权基于成员关系，所以 `owner` 与 `member` 目前对同一个 cell 的访问完全相同。把该字段当作元数据。 |
| cell（信任域） | 一个 `tenant_id` | 一个 workspace、一份记忆、一组会话与模型设置。域内的一切由所有成员共享。 |
| 成员关系 | `user_tenants(user_id, tenant_id, role, is_default)` | 授权某个账号访问某个 cell。多对多：一个账号可属于多个 cell，一个 cell 也可服务多个账号。 |

**什么是共享的、什么是按账号的。** 同一个 cell 的成员共享会话、聊天记录、记忆（FACTS / MEMORY / USER）、技能、项目目录与模型设置——这正是信任域的意义。凭据、refresh token 轮换与当前 cell 的选择则按账号/按浏览器：两个账号永远看不到对方的密码，但能看到对方的对话，因为对话属于 cell。

**切换与撤销。** 登录会选择账号的默认成员关系，并在认证 cookie 之外设置 HttpOnly 的 `sherry_tenant` cookie。`GET /auth/tenants` 列出全部成员关系，包含 `role`、`is_default` 和实时 `cell_status`；`POST /auth/tenant` 用实时表校验请求的 `tenant_id` 并重写 cookie。代理与 WebSocket 路由在每次请求时重新校验成员关系，因此 `user-tenant-remove` 立即生效——过期 cookie 回退到账号的默认 cell。SPA 的 `TenantSwitcher` 仅在账号拥有多个 cell 时出现，切换后整页重载，因为缓存的会话、记忆与 socket 都属于前一个 cell。

**开通速查。**

```bash
python -m fleet create --tenant team-a                     # 需要 OS 隔离时加 --backend container
python -m fleet user-add --username alice --password '...' --tenant team-a
python -m fleet user-tenant-add --username bob --tenant team-a --role member
python -m fleet user-tenants --username alice
python -m fleet user-tenant-remove --username bob --tenant team-a
```

第一个成员关系是 owner 与默认值。`rm` 只删除 cell、不删除成员关系，所以请先撤销或改派。

**用户之间的隔离。** cell **内部**没有隔离——cell 就是隔离单元。两个人不应看到对方会话时，给他们各自的 cell；这些 cell 属于不可信租户时使用 container 层（它提供文件系统、网络与 cgroup 边界）。容器 cell 的自身登录按构造关闭（`SHERRY_AUTH_ENABLED=false` 加一次强制关闭，见 container 章节），因为 Ingress 流量从 bridge 网关而非 loopback 到达——真正起作用的登录是 Ingress 的登录。

## 🧩 Cell 生命周期

`python -m fleet create --tenant alice` 会创建 `<fleet_root>/alice/{workspace,logs,sherry.jsonc}`，并分配 20100-20299 中第一个空闲端口。端口选择与注册写入由进程锁加 fleet 根目录的 `flock` 串行化，因此两个 CLI 进程不会注册同一个端口。`--backend process`（默认）记录 `runtime=process`；`--backend container` 记录 `runtime=container`、镜像与容器名，之后的每条命令都读取这一列，不必再次指定后端。

`start` 注入 `SHERRY_HOME`、`SHERRY_PROJECT_DIR`、`API_PORT`、`SHERRY_GATEWAY_TOKEN`、`PUBLIC_BASE_URL`、`SHERRY_DAEMON_HOME` 和 `SHERRY_DAEMON_CLIENT_ONLY`，以 detached 方式启动服务，并轮询 `/health` 直到预算耗尽。

`stop` 只会向环境中仍然写着该 cell `SHERRY_HOME` 的 PID 发信号；被复用的或外来的 PID 不会被触碰。`create` 还接受 `--seed-config`，用于复制宿主机的 `.models` 与 `.network` 文件（默认关闭，因为这些文件带有凭据）。

## 📦 Container 层（OS 级隔离）

`python -m fleet create --tenant alice --backend container --image sherry-agent:dev` 保持同一棵数据树和同一条 Ingress 路由，但 cell 运行在自己的容器里：

```text
--name sherry-cell-alice        --init          --restart unless-stopped
--cap-drop=ALL                  --security-opt no-new-privileges
--memory 4g --cpus 2            --pids-limit 1024
--network sherry-cell-alice-net --volume <fleet_root>/alice:/app/data
-p 127.0.0.1:20100:8080         --add-host host.docker.internal:host-gateway
--label sherry.fleet.tenant=alice --label sherry.fleet.attempt=<id>
```

一个 0600 的 env 文件携带 `SHERRY_HOME=/app/data`、`SHERRY_PROJECT_DIR=/app/data/workspace`、gateway token、`API_PORT`、`SHERRY_AUTH_ENABLED=false`、`PUBLIC_BASE_URL` 和 `LOCAL_DAEMON_HOST=host.docker.internal`；`create` 消费后立即删除该文件。容器镜像就是仓库里的 Dockerfile，无需改动，所以 `docker build` 或 `podman build` 是唯一的镜像步骤。

健康检查除了 `/health` 还会核对 attempt 标签：如果端口有响应但容器携带的 `sherry.fleet.attempt` 不是本次的，就视为残留容器——这样被引擎重启的旧容器无法冒充新容器。

**两种网络模式。** `SHERRY_FLEET_CONTAINER_NETWORK=publish`（Linux 与 macOS 的默认值）为每个租户创建独立 bridge 网络并发布 `127.0.0.1:<port>`，容器之间互不可达。`host` 共享运行时的网络命名空间，cell 直接监听分配到的端口——这是 WSL2 的兜底路径，因为 WSL2 内核不带 nftables 支持，netavark 无法应用端口发布规则（`auto` 在 Windows 宿主机上选 `host`，其他平台选 `publish`）。host 模式用网络隔离换取可移植性；内核允许时应保持 `publish`。

**cell 自身的登录按构造关闭。** Ingress 到容器的流量来自 bridge 网关而非 loopback，如果 cell 强制启用自身登录，它会对每个代理请求返回 `401`。新 cell 由 `SHERRY_AUTH_ENABLED=false` 把 `auth_settings.enabled` 播种为 0。**从备份恢复**的 cell 可能带回已启用的登录；manager 通过 `/auth/status` 发现后，直接在 `<data_dir>/src/data/auth.db` 里把该行改回关闭，并等待 cell 的 2 秒状态缓存过期。如果登录仍然生效，`create`/`start` 会明确报错，而不是留下一个谁都无法访问的 cell。

**升级与审计。** `python -m fleet upgrade --tenant alice --image <tag>` 停止容器，用新镜像、同一棵数据树重建并启动；新镜像健康检查失败时回滚到上一个镜像。`python -m fleet doctor [--tenant alice]` 逐个检查容器 cell：容器存在且运行、`cap-drop` 移除了引擎的核心能力集（podman 会展开成列表，docker 报告 `ALL`）、设置了 `no-new-privileges`、内存/CPU/pids 上限非零、fleet 标签与租户一致、发布绑定仅 `127.0.0.1` 或网络模式为 `host`、租户网络内只有一个容器、数据目录为 `0700`（POSIX）且 `/health` 有响应。任一检查失败，命令以 1 退出。

## 🛡️ 监督与资源限制

| 症状 | Supervisor 的处理 |
| :--- | :--- |
| 记录的 PID 消失，或其 `SHERRY_HOME` 不再匹配 | 回收陈旧的子进程并启动替代进程 |
| 进程存活但 `/health` 连续三次失败 | 先 `stop`，再 `start` |
| 停止的 cell 留下未回收的僵尸进程 | 替代进程绑定端口前用 `waitpid` 回收 |
| `logs/cell.log` 超过 `SHERRY_FLEET_LOG_MAX_BYTES` | 复制截断为 `cell.log.1`，对运行中的 O_APPEND 描述符安全 |
| cell 超过 `SHERRY_FLEET_CELL_MEMORY_MB` 或 `SHERRY_FLEET_CELL_NOFILE` | exec 包装脚本已应用 `ulimit`；CPU 配额仍属于 cgroup 或 systemd 的职责 |
| 容器 cell 处于 exited 或已丢失 | `start` 重建并启动它（数据树不受影响） |
| 容器 cell 在运行但 `/health` 连续三次失败 | 先 `stop`，再 `start` |

Supervisor 运行在 Ingress 内，每隔 `SHERRY_FLEET_SUPERVISE_INTERVAL_S` 秒探测一次所有运行中的 cell。`SHERRY_FLEET_SUPERVISE=0` 会关闭该循环，交由运维自行管理。

## 💾 备份与恢复

`python -m fleet backup --tenant alice --out <dir>` 复制 cell 目录树，跳过 `logs/`、`-wal` 和 `-shm`，并通过 `sqlite3.Connection.backup()` 快照每个 `.db`，因此 SQLite 文件在事务上是一致的。cell 运行期间，非数据库文件是尽力而为的复制。container 层使用同一棵宿主机目录树，所以备份与恢复方式不变。

`restore` 会先停止 cell，暂存备份，把当前目录树改名为 `<tenant>.pre-restore`，再把暂存副本换上。`python -m fleet backup-db --out <dir>` 备份中心的 `fleet.db`（用户、refresh token、cell 与进程记录）。

## 🤝 共享模型 daemon

`python -m fleet daemons-start` 在 `<fleet_root>/daemons` 下持有每个本地模型 daemon 的唯一副本（embed、reranker、本地对话、视觉）。cell 以 `SHERRY_DAEMON_CLIENT_ONLY=1` 运行：连接已记录的 pid，绝不启动第二份，也不注册 atexit kill。`SHERRY_DAEMON_HOME` 让每个 cell 指向共享的 pid 目录。

本地对话与视觉还需要 `SHERRY_FLEET_AUXILIARY_MODEL_PATH`、`SHERRY_FLEET_VISION_MODEL_PATH` 和 `SHERRY_FLEET_VISION_MMPROJ_PATH`。缺少这些变量时，命令会报告缺失项，而不是启动一个永远加载不了的 daemon。daemon 不在时，客户端回退到进程内加载器，功能仍可用，但内存不再按 cell 共享。

容器 cell 通过 `host.docker.internal` 连接宿主机 daemon，因此 daemon 必须监听运行时可达的地址（宿主机侧设置 `LOCAL_DAEMON_HOST=0.0.0.0`）。在 VM 型运行时的 host 网络模式下，容器的“宿主机”是 VM 而不是桌面系统——本地模型请留给 process cell 或 Linux 宿主机。

## 🖥️ CLI

```bash
python -m fleet serve
python -m fleet user-add --username alice --password '...' --tenant alice
python -m fleet user-tenant-add --username alice --tenant team-b --role member
python -m fleet user-tenants --username alice
python -m fleet user-tenant-remove --username alice --tenant team-b
python -m fleet create --tenant alice                                  # process 后端
python -m fleet create --tenant bob --backend container --image sherry-agent:dev
python -m fleet start --tenant alice
python -m fleet stop --tenant alice
python -m fleet upgrade --tenant bob --image sherry-agent:next          # 仅 container
python -m fleet doctor --tenant bob                                    # container 审计
python -m fleet list
python -m fleet backup --tenant alice --out /srv/backups/alice
python -m fleet restore --tenant alice --from /srv/backups/alice
python -m fleet backup-db --out /srv/backups/fleet
python -m fleet daemons-start
python -m fleet daemons-stop
python -m fleet daemons-status
```

## 🔐 安全边界

| 边界 | 部署保证 |
| :--- | :--- |
| 浏览器凭据 | HttpOnly、SameSite=Lax cookie；`/auth/token` 在 Ingress 上是 404 |
| Cell 凭据 | 每个 cell 一个 `SHERRY_GATEWAY_TOKEN`；浏览器提供的 token 与 ticket 一律剥离 |
| Cell 网络 | cell 只绑定 `127.0.0.1`；Ingress 通过 loopback 访问 |
| 租户文件（process） | `SHERRY_HOME` 分隔数据树，但所有 cell 共享同一 OS 用户；恶意租户需要 container 层 |
| 租户文件（container） | 租户数据树之上的私有挂载命名空间、`cap-drop=ALL`、`no-new-privileges` 与硬性内存/CPU/pids 上限；`terminal` 与 `python_repl` 读不到其他租户的数据 |
| 容器网络 | `publish` 模式为每个租户提供独立 bridge 且只发布 loopback 端口；容器之间互不可达 |
| 资源耗尽 | 容器 OOM 或 pids 风暴会被限制在自己的 cgroup 内，不影响 Ingress 与其他 cell |

- 同一租户内的多个账号共享该 cell 的会话与记忆；按用户隔离需要拆分租户。
- 容器 cell 在容器内部仍然信任自己的租户：边界是租户对租户，而不是用户对用户。

## 🧪 测试

`tests/fleet/` 覆盖 store、Ingress 认证、代理、WebSocket 与 TLS、manager 生命周期、共享 daemon 以及备份恢复。`test_cell_e2e.py` 通过 manager 启动真实的 `python -m server` cell，验证 `/health`、PID 身份、登录、HTTP 代理与干净的 SIGTERM。

container 后端新增 `test_container_manager.py`（可记录的假运行时：命令形状、env 文件、两种网络模式、健康失败清理、升级回滚、doctor 结论、监督循环以及恢复后关闭登录）与 `test_container_runtime_e2e.py`——一个可选的真实引擎冒烟测试，让实际运行时执行 `create`：

```bash
SHERRY_FLEET_CONTAINER_E2E=1 uv run --no-sync pytest tests/fleet/test_container_runtime_e2e.py
```

`pytest tests/fleet` 与仓库的拆分门禁是回归命令。客户端认证有独立的 store、登录页、guard 与账户面板测试套件，完整的后端门禁在独立进程中运行 unit、integration 和 regression 分组。

## 📌 运维要求

- 用 systemd 或 supervisord 托管 Ingress：它内置的 supervisor 管理的是 cell，而不是 Ingress 自身。
- 以同源方式构建 SPA（`VITE_API_BACK_URL=same-origin`），并把 `PUBLIC_BASE_URL` 设为浏览器可见的 Ingress 源。
- 设置稳定的 `SHERRY_INGRESS_JWT_SECRET`；每次启动随机生成会让所有 cookie 失效。
- 在 Ingress 或可信代理处终止 TLS，并把额外来源加入 `SHERRY_INGRESS_ALLOWED_ORIGINS`。
- 为 manager 未强制的限制补充 cgroup 或 systemd 的 CPU 配额，以及外部备份或 logrotate 定时任务。
- 每个租户绑定一个项目目录；未绑定会让两个租户共享 `ROOT_DIR/.sherry/coordination.db`。
- 每个账号至少保留一个成员关系；没有成员关系的登录会返回 `no_tenant`。
- container 层需要 Ingress 宿主机上有 Docker 或 Podman 可执行文件（`SHERRY_FLEET_CONTAINER_RUNTIME`）、已构建的镜像（`SHERRY_FLEET_IMAGE`、`SHERRY_FLEET_CONTAINER_PULL`），以及镜像之外的每空闲 cell 约 1 GB 内存；按宿主机实际容量设置 `SHERRY_FLEET_CONTAINER_MEMORY` / `_CPUS` / `_PIDS`。
- 在 POSIX 宿主机上，容器以 fleet 用户的 uid 运行，因此 `0700` 的数据树无需 chown 即可写；若要使用不同的运行时 uid，需要在镜像里支持 `--user`，或配置 rootless 的 subuid 映射。
