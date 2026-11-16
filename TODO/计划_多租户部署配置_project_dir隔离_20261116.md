# 计划：多租户部署配置 checklist——project_dir 隔离

- 日期：2026-11-16
- 状态：**待评审**
- 来源：多租户审计发现 coordination.db 和默认 project_dir 不随 `SHERRY_HOME` 移动——不是代码 bug，是部署配置要求
- 风险等级：**无** — 不改代码，纯部署配置
- 依赖：多租户 Fleet cell / bare-metal 部署

---

## 0. 问题

多租户部署中设 `SHERRY_HOME=/data/cells/<user>` 后，几乎所有持久化路径自动隔离。但有两个路径不随 `SHERRY_HOME` 移动：

### 缺口 1：coordination.db

**文件**：`agent/tools/pub_base/coordination.py:92-94`

```python
def coordination_db_path(project_root: Path) -> Path:
    return Path(project_root) / ".sherry" / "coordination.db"
```

coordination.db 锚在**项目目录**（`<project>/.sherry/coordination.db`），不随 `SHERRY_HOME` 移动。这是设计如此——它是 per-project 的协调 store，不是 per-user 的。

**冲突场景**：两个租户不设 `SHERRY_PROJECT_DIR`，都 fallback 到 `ROOT_DIR` → 共享 `ROOT_DIR/.sherry/coordination.db`。

### 缺口 2：resolve_default_project_dir() fallback

**文件**：`config/path.py:511-545`

```python
def resolve_default_project_dir() -> Path:
    configured = os.environ.get("SHERRY_PROJECT_DIR", "").strip()
    if not configured:
        configured = str(get_sherry_setting("project_dir") or "").strip()
    if not configured:
        return ROOT_DIR                    # ← fallback 到代码目录
    ...
```

优先级：`SHERRY_PROJECT_DIR` env → `sherry.jsonc` `project_dir` → `ROOT_DIR`。

当两个都不设时，fallback 到 `ROOT_DIR`（代码目录）。这是有文档的旧行为（"byte-for-byte pre-project-binding behaviour"），不是 bug。

**冲突场景**：同上——多个租户都 fallback 到 `ROOT_DIR`。

---

## 1. 为什么不改代码

### coordination.db

- 它已经是 per-project 的（`<project>/.sherry/coordination.db`）
- 如果两个租户绑定不同的项目目录，coordination.db 自然隔离
- 改成 per-user 反而错误——同一用户的多个 session 可能操作不同项目，coordination 是 per-project 的语义

### resolve_default_project_dir()

- fallback 到 ROOT_DIR 是有意的——保持"未配置时指向代码目录"的旧行为
- 已有 `project_dir_source()` 返回 `"session"` / `"env"` / `"default"`，可区分来源
- 改 fallback 到 APP_HOME 没有意义——APP_HOME 不是项目目录

**这两个都是部署配置问题，不是代码问题。**

---

## 2. 部署配置要求

### Fleet cell 模式

每个 cell 的容器 env 中注入：

```bash
SHERRY_HOME=/app/data                              # 数据卷（已覆盖）
SHERRY_PROJECT_DIR=/app/workspace                  # per-user 项目目录
SHERRY_GATEWAY_TOKEN=<per-cell-token>              # Gateway 认证
```

或通过 per-user `sherry.jsonc`（在 `SHERRY_HOME/sherry.jsonc` 中）：

```jsonc
{
  "project_dir": "/app/workspace"
}
```

`SHERRY_HOME` 已使 `sherry.jsonc` per-user（在 `/app/data/sherry.jsonc`），所以每 cell 的配置独立。

Fleet 创建 cell 时的工作：
1. 创建 `<host>/fleet/cells/<tenant>/` 目录
2. 创建 `<host>/fleet/cells/<tenant>/workspace/` 子目录（per-user 项目空间）
3. 写 `<host>/fleet/cells/<tenant>/sherry.jsonc`，设 `project_dir` 指向 `/app/workspace`
4. 容器挂载 `<host>/fleet/cells/<tenant>/` → `/app/data`
5. 容器内 `SHERRY_HOME=/app/data` → `sherry.jsonc` 在 `/app/data/sherry.jsonc` → `project_dir=/app/workspace`

### bare-metal 多进程模式

每个进程的 env：

```bash
SHERRY_HOME=/data/cells/<user_id>
SHERRY_PROJECT_DIR=/data/cells/<user_id>/workspace
SHERRY_GATEWAY_TOKEN=<per-user-token>
SHERRY_PORT=<per-user-port>
```

或 per-user `sherry.jsonc`：
```jsonc
{
  "project_dir": "/data/cells/<user_id>/workspace"
}
```

### Ingress / Fleet CLI 的职责

Fleet `create` 命令在创建 cell/进程时：
1. 创建数据目录 `<host>/fleet/cells/<tenant>/`
2. 创建项目目录 `<host>/fleet/cells/<tenant>/workspace/`
3. 生成 per-user `sherry.jsonc`（设 `project_dir`）
4. 注入 `SHERRY_HOME` + `SHERRY_PROJECT_DIR` env

---

## 3. 验证

每个租户启动后验证：

```bash
# 确认 project_dir 不指向 ROOT_DIR
curl http://127.0.0.1:<port>/sessions/project?session_id=<sid>
# 应返回 per-user 路径，不是代码目录

# 确认 coordination.db 在 per-user 项目目录下
ls <per-user-workspace>/.sherry/coordination.db
# 不应存在于 ROOT_DIR/.sherry/
```

---

## 4. 不做的事

- ❌ 不改 `coordination_db_path()`——per-project 是正确语义
- ❌ 不改 `resolve_default_project_dir()` 的 ROOT_DIR fallback——是设计行为
- ❌ 不自动创建 `.sherry/`——`maybe_create_sherry_dir` 只在 session 绑定时创建，process-default 不创建（fail-open 返回 None）

---

## 5. Kill criteria

- 无。纯部署配置，不涉及代码变更。
