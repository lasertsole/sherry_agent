# 计划:MCP 文件夹化(统一打包的后续计划)

本计划是统一打包(塞包模式)落地后的后续计划 —— 依赖打包改造后 `plugins/` 落入只读安装目录这一前提,解决"用户如何往只读包里加自己的 MCP"问题。

## 设计概览

把当前"单一 `plugins/mcp_server/config.json` 配置文件"模式,改成**用户可投放的 MCP 文件夹**:

1. **专用文件夹**:`<SHERRY_HOME>/mcp_servers/`(可写,per-user)
2. **投放即用**:用户把 MCP server 配置(可选 + server 文件)丢进文件夹,后端启动时自动扫描加载
3. **两种投放形态**(照搬 channels 的 `plugins/channels/<name>/config.json` 约定):
   - 简单扁平:`mcp_servers/<name>.json`(JSON 即 server spec)
   - 自包含子目录:`mcp_servers/<name>/config.json` + 可选 server 文件(`$here/` 指向子目录本身)
4. **叠加合并**:安装包内置默认 MCP(只读 `plugins/mcp_server/config.json`,当前为空)+ 用户文件夹(`mcp_servers/`),按 server name 合并,用户覆盖内置
5. **前端 UI**:ExtendPanel MCP tab 从空占位升级为"已发现 MCP 列表 + 打开文件夹按钮 + 重启后端生效"
6. **加载时机**:`build_main_tools()` 在 agent 构建时调一次(模块级 `_tools` 缓存,`agent/core.py:120`)—— **首版用"重启后端"策略**,不搞热加载(热加载要重建 agent 图,复杂度不值)

## 已确认的事实

| # | 事实 | 证据 |
|---|------|------|
| 1 | 当前 MCP 走**单一 JSON 文件**:`plugins/mcp_server/config.json`(当前空 `{}`) | `agent/tools/mcp_plugin.py:19` |
| 2 | `PLUGINS_PATH = ROOT_DIR / "plugins"`,`ROOT_DIR` 是 bundle 根(exe 父目录) | `config/path.py:83` |
| 3 | 统一打包下 `ROOT_DIR` 指向**只读安装目录** → `plugins/` 不可写,用户加不了 MCP | 已落地的 bundle.resources(A1) |
| 4 | 加载器 `_load_config()` 读单文件,`build_mcp_tools()` 建 `MultiServerMCPClient` 取所有工具 | `mcp_plugin.py:47-54` |
| 5 | 占位符 `$sys.executable`(当前 venv Python)、`$here/`(配置文件所在目录 `plugins/mcp_server/`) | `mcp_plugin.py:32-42` |
| 6 | MCP 工具命名 `mcp__<server>__<tool>`,自动归入 catalog 的 `mcp` 组(无需改 catalog) | `agent/tools/catalog.py:74-93` |
| 7 | `build_mcp_tools` 在 `_MAIN_TOOLS_BUILDERS` 链里(`__init__.py:58`),`build_main_tools()` 在 `agent/core.py:120` 调一次,结果存模块级 `_tools` | `agent/tools/__init__.py:75`、`agent/core.py:92,120` |
| 8 | channels 已用"每渠道一子目录 + config.json"约定(`plugins/channels/<name>/config.json`)—— MCP 照搬此约定保持一致 | `server/trigger/http/channels.py:162` |
| 9 | **无 MCP API 路由** —— 只有 channels 有(`server/trigger/http/channels.py`),MCP 需新建路由 | `ls server/trigger/http/` |
| 10 | 前端 ExtendPanel 有 MCP tab(tab index 1),当前只显示空占位"No MCP servers configured" | `client/app/pages/home/components/ExtendPanel.vue:132-142` |
| 11 | Tauri `tauri-plugin-opener = "2"` 已在依赖里,`lib.rs:45-47` 已用于"在文件管理器中显示" —— 打开 `mcp_servers/` 文件夹零成本 | `client/src-tauri/Cargo.toml:41` |
| 12 | `APP_HOME`(SHERRY_HOME)已有多个可写子目录约定:`src/`、`static/`、`temp/`、`models/`、`workspace/` —— 加 `mcp_servers/` 顺理成章 | `config/path.py:85-102` |

## 任务分解

### A. 后端路径与目录约定

#### A1. `config/path.py` 加 `MCP_SERVERS_DIR`

```python
#: 用户投放 MCP 的可写文件夹(per-user)。叠加在只读的 plugins/mcp_server/ 内置默认之上。
MCP_SERVERS_DIR = APP_HOME / "mcp_servers"
```

位置:紧跟 `MODELS_DIR = APP_HOME / "models"` 之后,保持可写子目录聚集。

#### A2. `config/__init__.py` 导出

`MCP_SERVERS_DIR` 加入 `__all__` / `from .path import ...` 块。

#### A3. 首启建目录

`MemoryStore.load_from_disk()` 或 `config/path.py` 的首启 seeding 逻辑里,加:
```python
MCP_SERVERS_DIR.mkdir(parents=True, exist_ok=True)
```
(参考 `.env` / `.models` / `.network` 的首启 seeding 模式,`config/path.py:28-32`)

### B. MCP 加载器重写(`agent/tools/mcp_plugin.py`)

#### B1. `_load_config()` 改文件夹扫描 + 合并

当前读单文件,改成:

```python
def _load_config() -> dict:
    """加载内置默认 + 用户文件夹,按 server name 合并(用户覆盖内置)。"""
    servers: dict[str, dict] = {}

    # 1. 内置默认(只读 bundle,向后兼容 + shipped defaults)
    bundled = _load_bundled_config()  # 原 plugins/mcp_server/config.json 逻辑
    servers.update(bundled)

    # 2. 用户文件夹(可写,扫描两种形态)
    user_dir = MCP_SERVERS_DIR
    user_dir.mkdir(parents=True, exist_ok=True)  # 首启保底
    for entry in sorted(user_dir.iterdir()):
        spec, here_dir = _parse_mcp_entry(entry)
        if spec is None:
            continue  # 非 .json / 非 config.json / 解析失败
        name = entry.stem if entry.is_file() else entry.name
        spec = _resolve_placeholders(spec, here_dir)
        servers[name] = spec  # 用户覆盖内置同名

    return servers
```

#### B2. `_parse_mcp_entry()` 支持两种形态

```python
def _parse_mcp_entry(entry: Path) -> tuple[dict | None, Path]:
    """返回 (server spec, $here 解析目录)。"""
    if entry.is_file() and entry.suffix == ".json":
        # 扁平:文件本身就是 server spec
        spec = json.loads(entry.read_text(encoding="utf-8"))
        return spec, entry.parent  # $here/ = mcp_servers/

    if entry.is_dir():
        cfg = entry / "config.json"
        if cfg.is_file():
            spec = json.loads(cfg.read_text(encoding="utf-8"))
            return spec, entry  # $here/ = 子目录本身(可放 server.py 等)

    return None, entry  # 跳过
```

#### B3. `$here/` per-server 解析

当前 `$here/` 固定指向 `plugins/mcp_server/`。改成 per-server:扁平文件 → 文件父目录;子目录 → 子目录本身。这让"用户把 `server.py` + `config.json` 一起丢进 `mcp_servers/my_tool/`"能 `args: ["$here/server.py"]` 正常解析。

#### B4. 错误隔离

单个 MCP 解析失败不应拖垮全部加载:try/except 每个 entry,失败的记 warning 跳过,不影响其它 server。当前 `_load_config()` 没有这种隔离(单文件解析失败全挂)。

### C. 后端 API 路由(新建 `server/trigger/http/mcp.py`)

#### C1. `GET /mcp/servers` —— 列出已发现的 MCP

```python
def register_mcp_routes():
    @app.get("/mcp/servers")
    def list_mcp_servers():
        """扫描 mcp_servers/ + 内置默认,返回 server 列表(不连接,只列)。"""
        servers = _scan_mcp_servers()  # 复用 mcp_plugin 的扫描逻辑,抽出为 mcp_service
        return {
            "servers": [
                {"name": name, "source": "bundled"|"user", "command": spec.get("command"),
                 "has_error": False, "error": None}
                for name, spec in servers.items()
            ],
            "user_dir": str(MCP_SERVERS_DIR),
        }
```

参考 `channels.py` 的 route 风格(同目录、同 import 模式)。

#### C2. `POST /mcp/reload` —— 触发重载(可选,首版可省)

由于 `build_main_tools()` 是模块级缓存 + agent 图持 tool list,**真正热加载需要重建 agent 图**。首版策略:`POST /mcp/reload` 只做"标记脏 + 提示用户重启后端",不实际重建。前端按钮直接调 Tauri 重启后端命令(见 E 节)。

如果后续要做真热加载,需要:
- `_tools` 改成可重置 + agent 图重建钩子
- 复杂度高,列为本计划外未来增强

#### C3. `mcp_service.py`(新建 `server/service/`)

抽出扫描逻辑(`_scan_mcp_servers()`)到 service 层,路由和 loader 共用,避免逻辑重复。

### D. 前端 ExtendPanel MCP tab 实现

#### D1. `ExtendPanel.vue` MCP tab 改造

当前(tab index 1)只是空占位。改为:

```
┌─────────────────────────────────────────────┐
│ MCP 服务器                                   │
│                                             │
│ [📂 打开文件夹]  [🔄 刷新]                   │
│                                             │
│ 已发现:                                     │
│ ┌─────────────────────────────────────────┐ │
│ │ ● filesystem   user   command: npx...   │ │
│ │ ● github       user   command: npx...    │ │
│ │ ○ my_tool     user   ⚠ 解析失败: JSON 错误 │ │
│ └─────────────────────────────────────────┘ │
│                                             │
│ 提示:把 MCP 配置文件丢进文件夹,重启后端生效。│
│ 支持两种形态:                                │
│ • mcp_servers/<name>.json(扁平)            │
│ • mcp_servers/<name>/config.json(自包含)   │
└─────────────────────────────────────────────┘
```

#### D2. `use-mcp-servers.ts`(新建 composable)

```typescript
const servers = ref<McpServer[]>([])
const userDir = ref<string>('')

async function refresh() {
  const res = await $fetch('/api/mcp/servers')
  servers.value = res.servers
  userDir.value = res.user_dir
}

onMounted(refresh)
```

#### D3. "打开文件夹"按钮

调用 Tauri `opener` 命令(已集成,`tauri-plugin-opener`),打开 `userDir` 在系统文件管理器:

```typescript
async function openFolder() {
  await invoke('open_path_in_manager', { path: userDir.value })
}
```

需在 `lib.rs` 暴露一个 `open_path_in_manager` Tauri command(参考已有的 reveal 逻辑,`lib.rs:45-47` 那段)。

#### D4. "重启后端"提示

投放新 MCP 后,UI 提示"需重启后端生效",提供"重启后端"按钮 → `invoke('restart_backend')`。

### E. Tauri:打开文件夹 + 重启后端命令

#### E1. `lib.rs` 暴露 `open_path_in_manager` command

参考已有 reveal 逻辑(AGENTS.md 提到 `@tauri-apps/plugin-opener` 用于"Reveal in the file manager"):

```rust
#[tauri::command]
fn open_path_in_manager(path: String) -> Result<(), String> {
    opener::open(path).map_err(|e| e.to_string())
}
```

注册到 `invoke_handler`。

#### E2. `restart_backend` command(可选)

杀掉 backend 进程 + 重新 spawn。`python_process.rs` 已有 `start()` / stop 逻辑,加一个 `restart()`:

```rust
#[tauri::command]
fn restart_backend(state: tauri::State<'_, PythonProcessManager>) -> Result<(), String> {
    state.stop();
    state.start().map_err(|e| e.to_string())?;
    Ok(())
}
```

注意:重启后 agent 图也会重建(因为 backend 是新进程),MCP 重新扫描生效。

### F. 打包:ship defaults + 首启建目录

#### F1. `build_backend.py` DATA 不变

`("plugins", "plugins")` 仍在 DATA 里 —— 内置 `plugins/mcp_server/config.json`(空,作 placeholder + 向后兼容)随包 ship。用户不可写的内置默认走这条路。

#### F2. 首启 seeding

`config/path.py` 首启逻辑加 `MCP_SERVERS_DIR.mkdir(...)`,并在 `mcp_servers/` 下放一个 `README.md` 说明投放约定(扁平 vs 子目录):

```python
if not (MCP_SERVERS_DIR / "README.md").exists():
    (MCP_SERVERS_DIR / "README.md").write_text(_MCP_FOLDER_README, encoding="utf-8")
```

README 内容:解释两种形态 + `$here/` 占位符 + 示例 config.json。

## 关键设计决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | MCP 用户文件夹放 `SHERRY_HOME/mcp_servers/`,**不**放 `plugins/mcp_server/` | 塞包模式下 `plugins/` 只读;SHERRY_HOME 可写且已是用户数据根 |
| D2 | 同时支持扁平 `.json` 和子目录 `config.json` 两种形态 | 扁平适合简单 server(一行 npx 命令);子目录适合自包含(带 server.py/binary),照搬 channels 约定 |
| D3 | 内置默认(`plugins/mcp_server/config.json`)+ 用户文件夹(`mcp_servers/`)叠加,用户覆盖同名 | 保留向后兼容;未来若 ship 内置 MCP(如 filesystem server)也走这条 |
| D4 | `$here/` per-server 解析(指向各自目录) | 让"server.py + config.json 一起投放"可行;当前全局 `$here/` 不支持 |
| D5 | 首版"重启后端"加载新 MCP,不做热加载 | `build_main_tools()` 模块级缓存 + agent 图持 tool list,热加载要重建图,复杂度不值;重启策略与 CUDA pack 一致 |
| D6 | 单 MCP 解析失败隔离,不拖垮全部 | 当前单文件失败全挂;文件夹模式必须容错(用户写的 JSON 可能错) |
| D7 | `GET /mcp/servers` 只扫描不连接(不 spawn MCP 进程) | 列表 API 应快、无副作用;真正连接发生在 `build_mcp_tools()` 调 `MultiServerMCPClient.get_tools()` 时 |

## 风险与开放问题

| # | 风险 | 严重度 | 缓解 |
|---|------|--------|------|
| R1 | channels 也写 `plugins/channels/config.json`,塞包模式下同样只读 → channels 也有写配置问题 | 高 | 本计划只解 MCP;channels 的写配置迁移是独立后续(建议同样搬到 SHERRY_HOME/channels/)。**前置计划落地前必须确认 channels 不崩** |
| R2 | MCP server 进程崩溃影响 agent | 中 | `MultiServerMCPClient` 已有连接管理;失败的 server 在 `get_tools()` 时报错,`build_mcp_tools` 应 try/each-server 隔离 |
| R3 | 用户投放的 MCP server 有恶意 command(如 `rm -rf`) | 中 | HITL 已有 untrusted_output 包装(`agent/security/untrusted_wrapper.py`);MCP 工具结果同样走 HITL 网关。文档提示用户只放信任的 MCP |
| R4 | `$here/` per-server 解析与旧 `plugins/mcp_server/config.json` 不兼容(旧全局 `$here/` 行为变) | 低 | 内置默认若用 `$here/` 现在解析为 `plugins/mcp_server/`(其所在目录),行为不变;只有用户文件夹的 `$here/` 改为 per-server |
| R5 | 文件夹扫描顺序非确定(server name 冲突时谁覆盖谁) | 低 | `sorted(user_dir.iterdir())` + 后扫覆盖先扫;文档说明同名时用户覆盖内置 |
| R6 | 大量 MCP 时 `MultiServerMCPClient.get_tools()` 启动慢(每个 server 一次连接) | 中 | 文档提示;未来可加 lazy connect(按需连接而非启动全连) |
| R7 | `POST /mcp/reload` 真热加载需重建 agent 图 | 高(若要做) | 首版不做,走重启后端;列为未来增强 |

## 不在本次范围

- **channels 写配置迁移到 SHERRY_HOME** —— 独立后续(R1),但塞包模式落地前必须确认
- **MCP 热加载(不重启生效)** —— 需重建 agent 图,首版走重启
- **MCP server 配置 wizard(UI 表单新建 config.json)** —— 首版只支持"打开文件夹手动投放";wizard 是增强
- **MCP server 健康检查/心跳** —— 首版不做,列表 API 只扫不连

## 落地顺序

### 阶段 1(可与塞包计划阶段 1 并行):后端加载器 + API

1. A1+A2 `config/path.py` 加 `MCP_SERVERS_DIR` + 导出
2. A3 首启建目录 + seeding README
3. B1+B2+B3+B4 `mcp_plugin.py` 重写(文件夹扫描 + per-server `$here/` + 错误隔离)
4. C3 `mcp_service.py` 抽扫描逻辑
5. C1 `GET /mcp/servers` 路由
6. 单测:`tests/agent/tools/test_mcp_plugin.py`(扫扁平 + 子目录 + 错误隔离 + 用户覆盖内置)

### 阶段 2:前端 UI

7. D2 `use-mcp-servers.ts`
8. D1 `ExtendPanel.vue` MCP tab 改造(列表 + 状态)
9. E1 `open_path_in_manager` Tauri command
10. D3 "打开文件夹"按钮接线
11. E2 `restart_backend` command + D4 "重启后端"按钮

### 阶段 3:打包集成(依赖塞包计划落地)

12. F1 确认 `build_backend.py` DATA 仍 ship `plugins/`(内置默认)
13. F2 首启 seeding `mcp_servers/README.md`
14. 端到端:装塞包版 → 打开 `mcp_servers/` → 投放一个 test MCP → 重启后端 → 工具 tab 看到 `mcp__<server>__*` 工具

## 测试清单

### 阶段 1 测试

- [ ] `mcp_servers/` 在 SHERRY_HOME 下被首启创建
- [ ] `mcp_servers/README.md` 被 seeding
- [ ] 投放扁平 `mcp_servers/test.json` → `GET /mcp/servers` 列出 `test`
- [ ] 投放子目录 `mcp_servers/test2/config.json` → 列出 `test2`
- [ ] `$here/` 在子目录形态指向子目录本身(`mcp_servers/test2/server.py` 可被 `args: ["$here/server.py"]` 解析)
- [ ] 投放坏 JSON → 该 server 标记 `has_error`,其它 server 不受影响
- [ ] 内置默认 `plugins/mcp_server/config.json` 有 server + 用户同名 → 用户覆盖内置
- [ ] `build_mcp_tools()` 返回的工具命名 `mcp__<server>__<tool>`,归入 catalog `mcp` 组

### 阶段 2 测试

- [ ] ExtendPanel MCP tab 列出已发现 server(名字 + source + command)
- [ ] 坏 server 显示 ⚠ 错误标记
- [ ] "打开文件夹"按钮在 Windows/macOS/Linux 都能打开 `mcp_servers/`
- [ ] "重启后端"按钮杀掉并重新 spawn backend
- [ ] 重启后新投放的 MCP 出现在工具 tab

### 阶段 3 测试(端到端,塞包版)

- [ ] 塞包版安装后 `mcp_servers/` 在 `%APPDATA%\com.ema-ai.agent\mcp_servers\`
- [ ] 投放一个真实 MCP(如 `filesystem.json`:`{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/tmp"]}`)
- [ ] 重启后端 → 工具 tab 出现 `mcp__filesystem__*` 工具
- [ ] agent 能调用该 MCP 工具完成对话

## 与前置计划的依赖关系

| 依赖项 | 来自前置计划 | 影响 |
|--------|-------------|------|
| 塞包模式下 `plugins/` 只读 | T1 `bundle.resources` + T2 `resource_dir()` | **本计划存在的前提** —— 若 `plugins/` 可写,用户直接改 `plugins/mcp_server/config.json` 即可,无需文件夹化 |
| `SHERRY_HOME` 改 CLI 参数 | D1 `--sherry-home` | `MCP_SERVERS_DIR = APP_HOME / "mcp_servers"` 依赖 `APP_HOME` 正确解析,CLI 参数化后路径仍准 |
| 塞包版首启 seeding 机制 | A3 首启建目录 | `mcp_servers/` 走同一 seeding 路径 |

**结论**:本计划必须在塞包模式落地后做(否则 `plugins/` 可写,文件夹化无意义);但**阶段 1(后端加载器 + API)可在塞包模式落地前先做** —— 加载器同时扫 `plugins/mcp_server/config.json`(老路径)和 `mcp_servers/`(新路径),老模式不破坏,新模式就绪。塞包模式落地后 `mcp_servers/` 自然成为唯一可写入口。
