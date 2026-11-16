# 计划 P3：Fleet cell 容器化多租户——OS 级隔离

- 日期：2026-11-16
- 状态：**待评审**
- 定位：**第二级多租户**。每租户一个 Docker/Podman 容器，OS 级隔离（独立文件系统/网络/进程命名空间）。在 P2 基础上升级，Ingress 和 Fleet 管理器复用。
- 风险等级：**中** — 新增 Fleet container manager (~1,400 行) + Dockerfile 调整 + 本地模型 daemon 共享配置
- 依赖：P1（daemon host env override + LDW journal）+ P2（Ingress Gateway + Fleet manager 基础）
- 可独立执行：❌ 在 P2 完成后升级

---

## 0. 与 P2 的关系

P3 不是重写——是在 P2 的 Fleet manager 和 Ingress 基础上**加容器后端**：

| 组件 | P2 (bare-metal) | P3 (container) |
|------|-----------------|----------------|
| Ingress Gateway | ✅ 复用，不改 | 同 |
| Fleet manager | `--backend process` | 加 `--backend docker` |
| 进程管理 | supervisord/systemd | Docker/Podman |
| 隔离强度 | 路径隔离（弱） | 容器隔离（强） |
| 本地模型 daemon | `127.0.0.1` 直连 | `LOCAL_DAEMON_HOST=host.docker.internal` |
| `PUBLIC_BASE_URL` | ✅ 已在 P2 加 | 同 |
| Cell auth | loopback 自动 pass | 需 `SHERRY_AUTH_ENABLED=false`（Ingress IP 非 loopback） |

---

## 1. 架构

```
┌──────────────────────── Fleet Host ───────────────────────┐
│                                                             │
│  ┌─── Cell: user-A (容器) ───┐  ┌─── Cell: user-B ───────┐ │
│  │  cap-drop=ALL              │  │  cap-drop=ALL           │ │
│  │  no-new-privileges         │  │  no-new-privileges      │ │
│  │  mem=4g cpu=2 pids=1024    │  │  mem=4g cpu=2 pids=1024 │ │
│  │  独立 bridge network       │  │  独立 bridge network    │ │
│  │  Sherry :8080 → 127.0.0.1:20100 │  Sherry :8080 → :20101 │ │
│  │                            │  │                         │ │
│  │  挂载: /app/data ← host    │  │  挂载: /app/data ← host │ │
│  │  LOCAL_DAEMON_HOST=        │  │  LOCAL_DAEMON_HOST=     │ │
│  │    host.docker.internal   │  │    host.docker.internal │ │
│  │  SHERRY_HOME=/app/data     │  │  SHERRY_HOME=/app/data  │ │
│  │  SHERRY_AUTH_ENABLED=false │  │  SHERRY_AUTH_ENABLED=false│ │
│  └────────────────────────────┘  └─────────────────────────┘ │
│                                                             │
│  ┌─── 本地模型 daemon (host) ──────────────────────┐       │
│  │  LOCAL_DAEMON_HOST=0.0.0.0                        │       │
│  │  Embed :9012 / Reranker :9013 / LLM :9014        │       │
│  │  cell 通过 host.docker.internal 连接              │       │
│  └───────────────────────────────────────────────────┘      │
│                                                             │
│  ┌─── Ingress Gateway ──────────────────────────────┐      │
│  │  同 P2，复用                                       │      │
│  └───────────────────────────────────────────────────┘      │
│                                                             │
│  sherry-fleet --backend docker 管理容器                   │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 隔离对比

| 维度 | P2 bare-metal | P3 container |
|------|---------------|--------------|
| 进程 | 独立进程（共享内核） | 独立容器（独立命名空间） |
| 文件系统 | 路径隔离（能读对方） | 独立挂载（不能读对方） |
| 网络 | 共享（不同端口） | 独立 bridge（容器间不可达） |
| terminal/python_repl | ❌ 能读对方文件 | ✅ 容器内隔离 |
| 资源限制 | ulimit（手动） | `--memory`/`--cpus`/`--pids-limit` |
| 故障隔离 | 进程 OOM 不影响其他 | 容器 OOM 不影响其他 |
| 隔离阶梯 | 无法升级 | 容器 → gVisor → Kata → VM |

**定位：不可信租户。** 容器级隔离使 terminal/python_repl 的代码执行面安全隔离。

---

## 3. Dockerfile（已存在，微调）

现有 Dockerfile（`python:3.13-slim` + uv + 多阶段构建）已设 `ENV API_HOST=0.0.0.0`。需加：

```dockerfile
# 已有
ENV API_HOST=0.0.0.0
EXPOSE 8080

# 新增
ENV SHERRY_AUTH_ENABLED=false
# LOCAL_DAEMON_HOST 和 PUBLIC_BASE_URL 由 Fleet 创建时注入 env 文件，不在 Dockerfile 固定
```

注意：`SHERRY_AUTH_ENABLED=false` 只在 fresh DB 上生效（seed）。Fleet create 流程中 cell 启动后需调 `PUT /auth/enabled {"enabled": false}` 确保 runtime 也禁用。

本地模型镜像：现有 Dockerfile 排除 torch/llama-cpp-python（cloud-LLM 模式）。本地模型 cell 需单独 build target（加 torch + llama-cpp-python + gcc/g++/cmake），作为可选 `Dockerfile.local-models`。

---

## 4. 容器配置

参考 openclaw 的 `cell-profile.ts`：

```bash
docker create \
  --name sherry-cell-<tenant_id> \
  --init \
  --cap-drop=ALL \
  --security-opt no-new-privileges \
  --memory 4g \
  --cpus 2 \
  --pids-limit 1024 \
  --restart unless-stopped \
  --network sherry-cell-<tenant_id>-net \
  -p 127.0.0.1:<host_port>:8080 \
  --volume <data_dir>:/app/data \
  --add-host host.docker.internal:host-gateway \
  --env-file <temp_env_file> \
  --label sherry.fleet.tenant=<tenant_id> \
  --label sherry.fleet.attempt=<attempt_id> \
  <sherry_image>
```

**env 文件**（mode 0600，传入后删除）：
```bash
SHERRY_HOME=/app/data
SHERRY_PROJECT_DIR=/app/data/workspace
SHERRY_GATEWAY_TOKEN=<per-cell-token>
API_HOST=0.0.0.0
API_PORT=8080
SHERRY_AUTH_ENABLED=false
PUBLIC_BASE_URL=https://sherry.example.com
LOCAL_DAEMON_HOST=host.docker.internal
# *_DAEMON_ENABLED 默认 false（方案 C）；按需开启
```

注意：`SHERRY_AUTH_ENABLED=false` 只在 fresh DB 上生效（first-boot seed）。Fleet 创建 cell 后需通过 API 调 `PUT /auth/enabled {"enabled": false}` 确保 DB 层也禁用（见 §5）。

---

## 5. Cell auth（与 P2 不同）

**问题**：P2 bare-metal 模式下 Ingress 连 `127.0.0.1:<port>` → `is_loopback` = True → loopback 豁免，auth 自动通过。P3 容器模式下 Docker port forward 的 peer IP 可能不是 loopback（取决于 Docker 网络配置），`is_loopback` 可能返回 False。

**`SHERRY_AUTH_ENABLED=false` 不够**：该 env 是 first-boot seed（`auth_store.seed_auth_enabled_once`），只在新 DB 无行时生效。已有 DB 行不受 env 影响。runtime enforcement 读 SQLite `auth_settings.enabled`。

**解决方案**：Fleet 创建 cell 后，通过 cell 的 API 在 DB 层禁用 auth：

```python
# Fleet create 流程中，cell 启动后调一次
requests.put(f"http://127.0.0.1:{port}/auth/enabled", json={"enabled": False},
             params={"token": gateway_token})
```

或确保 cell 的 DB 是全新的（fresh DB + `SHERRY_AUTH_ENABLED=false` seed → `auth_settings.enabled=0`）。

Gateway token 仍保留（`SHERRY_GATEWAY_TOKEN` env）——Ingress 用它认证到 cell 的连接。`/health` 豁免 token 检查（`auth.py:64`）。

---

## 6. 本地模型 daemon 共享

P3 容器模式下 daemon 在 host 上，cell 在容器内：

- host daemon：`LOCAL_DAEMON_HOST=0.0.0.0`（P1 已加 env override）
- cell 容器：`LOCAL_DAEMON_HOST=host.docker.internal` + `--add-host host.docker.internal:host-gateway`
- daemon 端口 9012-9015 在 host 上，cell 通过 `host.docker.internal:<port>` 连接

**Docker Desktop (macOS/Windows)**：`host.docker.internal` 自动提供。
**Linux Docker**：需 `--add-host host.docker.internal:host-gateway`（Docker 20.10+）。

所有 cell 共享 host 上的一组 daemon → 模型权重只加载一次。

---

## 7. Fleet Container Manager（新增 ~1,400 行）

### 7.1 与 P2 Fleet manager 的关系

P2 的 Fleet manager 支持 `--backend process`。P3 加 `--backend docker`，复用：
- `fleet_cells` 表（加 `image` / `runtime` / `container_name` 列）
- 端口分配逻辑
- 健康检查逻辑
- backup/restore 逻辑

### 7.2 新增容器生命周期

```bash
sherry-fleet create --user alice --backend docker [--image <tag>]
sherry-fleet list
sherry-fleet status --user alice
sherry-fleet stop --user alice
sherry-fleet start --user alice
sherry-fleet rm --user alice [--purge-data]
sherry-fleet upgrade --user alice --image <tag>
sherry-fleet backup --user alice
sherry-fleet restore --user alice --backup <path>
sherry-fleet doctor
```

### 7.3 创建流程

1. 生成 tenant_id + gateway_token + attempt_id
2. 分配端口（20100+ 扫描）
3. 创建 host 目录 `<state-dir>/fleet/cells/<tenant>/`（mode 0700，拒绝 symlink）
4. 创建 workspace 子目录 + per-user `sherry.jsonc`
5. 创建独立 bridge network `sherry-cell-<tenant>-net`
6. 写 env 文件（mode 0600）
7. `docker create`（cap-drop=ALL, no-new-privileges, 资源限制, 挂载, 网络, 端口）
8. `docker start`
9. 健康检查（轮询 `/health` 60s，验证 attempt label）
10. 失败清理（删容器 + 删网络 + 释放端口）

### 7.4 升级流程

1. 拉新镜像
2. 从旧容器 inspect 出 token + env
3. stop + rm 旧容器
4. `docker create` + `docker start` 新容器（复用同一 data 卷）
5. 健康检查
6. 失败 → 回滚旧镜像 + 旧 attempt_id

### 7.5 doctor 检查

参考 openclaw 的 `doctor.runtime.ts`，检查每个 cell：
- 容器存在 + 运行中
- cap-drop=ALL + no-new-privileges
- 内存/CPU/PID 限制
- 端口绑定（`127.0.0.1:<port>` only）
- 网络隔离（独立 bridge，无额外容器附着）
- 数据目录权限（mode 0700）
- `/health` 响应

---

## 8. OS 支持

| 平台 | 运行时 | 状态 |
|------|--------|------|
| Linux | Docker / Podman | ✅ 原生支持 |
| macOS | Docker Desktop | ✅ Docker Desktop Linux VM |
| Windows | Docker Desktop (WSL2) | ⚠️ openclaw 标"untested"，代码支持 npipe。最干净路径：WSL2 内跑 |
| Windows | Podman | ❌ 不支持（`serviceIsRemote` 可能被拒） |

**Podman 仅 Linux/macOS。Windows 用 Docker Desktop。**

---

## 9. 执行计划

| 阶段 | 内容 | 时间 |
|------|------|------|
| 1 | Dockerfile 微调（env 注入） | ~1 天 |
| 2 | Fleet container manager（docker create/start/stop/rm + 网络 + 健康检查） | ~1 周 |
| 3 | upgrade/backup/restore（容器版） | ~3 天 |
| 4 | doctor 检查 | ~2 天 |
| 5 | 本地模型 daemon 共享配置（host daemon + cell 连接） | ~1 天 |
| 6 | 测试 + 文档 | ~3 天 |

**总计：~3 周（在 P2 基础上）**

---

## 10. 风险

| 风险 | 严重性 | 缓解 |
|------|--------|------|
| Docker Desktop on Windows 未验证 | Medium | WSL2 内跑最干净（`process.platform=linux`）；或降级为 P2 bare-metal |
| per-cell 4g 内存 | Medium | 默认 cloud-LLM 模式（方案 C）；本地模型用共享 daemon（方案 A） |
| Docker 镜像大小 | Low | Python + uv ~500MB-1GB，不含模型权重 |
| WS 反代延迟 | Low | loopback 反代 <1ms |
| SQLite backup 一致性 | Medium | `sqlite3.Connection.backup()` API（同 P2） |

---

## 11. Kill criteria

- 若 Docker Desktop on Windows 不可用且 bare-metal (P2) 满足需求 → 不做 P3，P2 即终态
- 若 per-cell 内存开销在目标 host 上不可接受（<4 cell）→ 评估应用级多租户（r1 计划）作为轻量替代
- 若本地模型 daemon 跨容器连接不稳定 → 禁用本地模型（方案 C），仅支持云模型
