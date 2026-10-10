# 计划：搜索后端可配 + Tavily 降级 SearXNG + WS 通知

- 建档日期：2026-10-09
- 状态：**计划（未开工）**
- 触发问题：sherry 的 web_search 只用 Tavily，API key 失效时直接返回"unavailable"。用户无法选搜索后端。降级时前端无感知

## 目标

1. 用户可在前端设置面板**主动选首选搜索后端**（Tavily / SearXNG）
2. 选 Tavily 时，Tavily 失效→**自动降级到 SearXNG**→**WS 推送通知前端**"Tavily 无法使用，已切换到 SearXNG"
3. 选 SearXNG 时，SearXNG 为主力（不 fallback 到 Tavily——自托管场景下 Tavily 可能也没配）
4. 对模型不可见：`web_search` 仍是一个工具，后端切换透明

## 现状

```python
# agent/tools/web_search.py
build_web_search_tool():
    if tavily_api_key:  → TavilySearch + retry
    else:               → stub "unavailable"
```

无 SearXNG。无 fallback。无 WS 通知。无用户选择。

## 设计

### 1. 搜索后端偏好（per-session，可全局默认）

#### 1a. State Key

`runtime/session/state_keys.py` 新增：

```python
SEARCH_BACKEND = "search_backend"  # "tavily" | "searxng" | None
```

`None` = 用全局默认（见 1b）。显式设值 = per-session 覆盖。

#### 1b. 全局默认

`config/features/infra_side/` 新增 `web_search.py`：

```python
class WebSearchConfig(TypedDict):
    default_backend: str       # "tavily"（默认）；有 SEARXNG_URL 无 TAVILY_API_KEY 时自动用 "searxng"
    searxng_proxy: str         # httpx 代理（e.g. "http://127.0.0.1:7890"），空=不代理
    searxng_engines: str       # 逗号分隔的引擎名（e.g. "google,bing,duckduckgo"），空=用 SearXNG 默认
    searxng_categories: str    # 搜索类别（e.g. "general", "it", "science"），空=general
    searxng_verify_tls: bool   # TLS 校验（代理是 MITM 证书时可关），默认 True

WEB_SEARCH_CONFIG: WebSearchConfig = {
    "default_backend": "tavily",
    "searxng_proxy": "",
    "searxng_engines": "",
    "searxng_categories": "",
    "searxng_verify_tls": True,
}
```

对应环境变量（`.env`，gitignored）：

```bash
SEARXNG_URL=http://localhost:8080           # SearXNG 实例地址
SEARXNG_PROXY=http://127.0.0.1:7890         # 可选：httpx 代理（科学上网）
SEARXNG_ENGINES=google,bing,duckduckgo      # 可选：指定搜索引擎
SEARXNG_CATEGORIES=general                  # 可选：搜索类别
SEARXNG_VERIFY_TLS=true                     # 可选：TLS 校验（代理 MITM 证书时关）
```

这些是**全局配置**（非 per-session）——SearXNG 实例的连接参数不随 session 变。前端通过 `GET /sherry_config` + `PUT /sherry_config` 读写（复用现有 sherry.jsonc 配置服务）。

运行时解析逻辑（在 `build_web_search_tool` 或工具内部）：

```python
def _resolve_preferred_backend(session_id: str) -> str:
    """1. per-session StateRegister → 2. config default → 3. 可用性兜底"""
    # 1. per-session 覆盖
    from runtime.session.state_register import state_register_mem
    explicit = state_register_mem.get_state(session_id, StateKey.SEARCH_BACKEND)
    if explicit in ("tavily", "searxng"):
        return explicit
    # 2. 全局默认
    default = WEB_SEARCH_CONFIG["default_backend"]
    # 3. 可用性兜底：默认是 tavily 但 key 没配 → 自动用 searxng（如果配了）
    if default == "tavily" and not tavily_available() and searxng_available():
        return "searxng"
    if default == "searxng" and not searxng_available() and tavily_available():
        return "tavily"
    return default
```

#### 1c. HTTP 端点（复用 session_settings 模式）

`server/trigger/http/session_settings.py` 新增（照 `PUT /sessions/thinking` 的形态）：

```python
# PUT /sessions/search_backend  {"session_id": <sid>, "backend": "tavily"|"searxng"|null}
async def put_search_backend_handler(request):
    session_id = request.query_params.get("session_id", "")
    backend = (request.json or {}).get("backend")
    if backend not in ("tavily", "searxng", None):
        return {"status_code": 400, "body": {"error": "backend must be 'tavily', 'searxng', or null"}}
    # park/promote 模式（同 thinking/model）：运行中的 turn 设为 pending，边界提升
    state_register_db.set_state(session_id, StateKey.SEARCH_BACKEND, backend)
    return {"backend": backend}

# GET /sessions/search_backend?session_id=<sid>
async def get_search_backend_handler(request):
    session_id = request.query_params.get("session_id", "")
    backend = state_register_mem.get_state(session_id, StateKey.SEARCH_BACKEND)
    if backend not in ("tavily", "searxng"):
        backend = _resolve_preferred_backend(session_id)
    return {"backend": backend, "tavily_available": tavily_available(), "searxng_available": searxng_available()}
```

#### 1d. 前端：菜单内新增"模型联网"入口

在左侧菜单中新增一个独立条目"模型联网"，作为所有联网搜索配置的统一面板。不塞进现有"工具"或"设置"面板——联网搜索是一个独立关注面，值得独占一个菜单入口。

**入口形态**：
- 左侧菜单：一个条目"模型联网"，与其他菜单项（账户、预设角色等）平级
- 点击后打开右侧 sidebar tab（与 工具 / 中间件 / 代理模型 tab 同形态）

**面板内容**（分三个 section）：

**Section 1：搜索引擎（per-session，`GET/PUT /sessions/search_backend`）**

Radio 单选：

| 选项 | 条件 | 说明 |
|---|---|---|
| ☉ Tavily（推荐） | `TAVILY_API_KEY` 已配 | 付费 API，结果质量高；失效时自动降级到 SearXNG |
| ☉ SearXNG（自托管） | `SEARXNG_URL` 已配 | 免费开源自托管；不依赖第三方 API key |

- 未配的选项灰禁 + 提示"未配置，点击配置 →"
- 两个都没配 → 整个 section 显示"未配置任何搜索引擎，web_search 工具不可用"
- 当前生效的后端高亮标注（如 Tavily 降级到 SearXNG 后标注"当前：SearXNG（Tavily 降级）"）

**Section 2：SearXNG 连接（全局，`GET/PUT /sherry_config`，仅在选了 SearXNG 或 SearXNG 已配时展开）**

| 配置项 | 字段 | UI 控件 | 说明 |
|---|---|---|---|
| 实例地址 | `SEARXNG_URL` | 文本输入 | SearXNG 实例 URL（e.g. `http://localhost:8080`） |
| 代理 | `SEARXNG_PROXY` | 文本输入 | httpx 代理（e.g. `http://127.0.0.1:7890`），空=直连。国内访问远程 SearXNG 或让 SearXNG 走代理连 Google 时用 |
| 搜索引擎 | `SEARXNG_ENGINES` | 文本输入（逗号分隔） | e.g. `google,bing,duckduckgo`；空=用 SearXNG 默认引擎集。附提示行：`常用：google, bing, duckduckgo, wikipedia, github, stackoverflow` |
| 搜索类别 | `SEARXNG_CATEGORIES` | 下拉选择 | `general` / `it` / `science` / `files` / `images` / `news`；默认 `general` |
| TLS 校验 | `SEARXNG_VERIFY_TLS` | 开关 | 默认开；代理用 MITM HTTPS 证书（Clash/V2Ray）时关 |

保存按钮 = `PUT /sherry_config`（复用现有配置服务）。

**Section 3：Tavily 状态（只读，当 Tavily 已配时显示）**

- API Key 状态：`TAVILY_API_KEY` 已配置 / 未配置
- 测试连接按钮（可选）：调一次 Tavily 搜索 `test query`，显示成功/失败 + 延迟

**降级通知的 UI 表现**：

当 WS 收到 `search_backend_fallback` 帧时：
- 顶部工具栏的 🌐 图标闪一下 + 角标变橙（需用户点击确认消角标）
- ChatBox 顶部出现一个非阻塞的 banner 条："⚠️ Tavily 无法使用（tavily_auth_failed），已切换到 SearXNG"
- 搜索结果的 tool result 卡片标注"via SearXNG"（工具返回文本里已含 "SearXNG search results" 前缀，前端可识别）

### 2. SearXNG 搜索函数

新建 `agent/tools/web_search_searxng.py`（~100 行），API 契约从 hermes-agent 提取，增加 proxy + engines 支持：

```python
"""SearXNG fallback search — self-hosted, no API key needed.

API contract mirrors hermes-agent's plugins/web/searxng/provider.py:
GET {SEARXNG_URL}/search?q=...&format=json&pageno=1 → results[].{title,url,content,score}

Adds: proxy support (科学上网), engine/category selection, TLS toggle.
"""

import os
import httpx
from loguru import logger
from config.features import WEB_SEARCH_CONFIG
from config.features import TOOLS_TIMEOUTS

_TIMEOUT = TOOLS_TIMEOUTS["web_search_timeout_seconds"]


def _searxng_url() -> str:
    return os.getenv("SEARXNG_URL", "").rstrip("/")


def _proxy() -> str | None:
    p = WEB_SEARCH_CONFIG.get("searxng_proxy", "") or os.getenv("SEARXNG_PROXY", "")
    return p.strip() or None


def _verify_tls() -> bool:
    raw = WEB_SEARCH_CONFIG.get("searxng_verify_tls", True)
    if isinstance(raw, bool):
        return raw
    return str(raw).lower() not in ("0", "false", "no", "off")


def _engines() -> str:
    return WEB_SEARCH_CONFIG.get("searxng_engines", "") or os.getenv("SEARXNG_ENGINES", "")


def _categories() -> str:
    return WEB_SEARCH_CONFIG.get("searxng_categories", "") or os.getenv("SEARXNG_CATEGORIES", "")


def searxng_available() -> bool:
    """SEARXNG_URL 非空（不发网络请求）。"""
    return bool(_searxng_url())


def searxng_search(query: str, limit: int = 5) -> str:
    """Search via SearXNG. Returns formatted string matching Tavily's output shape.
    Never raises — returns error string on failure."""
    base = _searxng_url()
    if not base:
        return "SearXNG fallback unavailable: SEARXNG_URL is not set."

    params: dict[str, str] = {"q": query, "format": "json", "pageno": "1"}
    engines = _engines()
    if engines:
        params["engines"] = engines
    categories = _categories()
    if categories:
        params["categories"] = categories

    try:
        # proxy 可以是 "http://127.0.0.1:7890" 或 None（直连）
        # verify=False 当代理用 MITM 自签证书时（如 Clash 的 HTTPS MITM）
        with httpx.Client(
            proxy=_proxy(),
            verify=_verify_tls(),
            timeout=_timeout,
        ) as client:
            resp = client.get(
                f"{base}/search",
                params=params,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("SearXNG HTTP {}: query={}", exc.response.status_code, query)
        return f"SearXNG search failed (HTTP {exc.response.status_code})."
    except httpx.RequestError as exc:
        logger.warning("SearXNG unreachable: {} (proxy={})", exc, _proxy() or "direct")
        proxy_hint = f" via proxy {_proxy()}" if _proxy() else " (direct connection)"
        return f"SearXNG search failed (could not reach {base}{proxy_hint}): {exc}"
    except Exception:
        logger.warning("SearXNG response parse failed", exc_info=True)
        return "SearXNG search failed (invalid response)."

    raw = data.get("results", [])
    ranked = sorted(raw, key=lambda r: float(r.get("score", 0)), reverse=True)[:limit]

    if not ranked:
        return f"No results found for '{query}'."

    lines = [f"SearXNG search results for '{query}' ({len(ranked)} results):\n"]
    for i, r in enumerate(ranked):
        title = str(r.get("title", ""))
        url = str(r.get("url", ""))
        content = str(r.get("content", ""))
        lines.append(f"[{i + 1}] {title}")
        lines.append(f"    URL: {url}")
        if content:
            lines.append(f"    {content}")
        lines.append("")

    return "\n".join(lines)
```

**proxy + engines 设计要点**：

- **proxy 是 httpx 的 `proxy` 参数**（不是 `proxies`），传 `http://127.0.0.1:7890` → 所有请求走代理。`None` = 直连。国内场景：SearXNG 实例在国外，sherry 通过代理访问实例；或 SearXNG 实例在本地但实例自己通过代理连 Google（后者是 SearXNG 自身的 `outgoing.proxies` 配置，不归 sherry 管）
- **engines 透传为 SearXNG 的 query 参数** `&engines=google,bing`。SearXNG 支持按引擎过滤结果。不指定 = 用实例默认引擎集
- **categories 同理**：`&categories=general` / `it` / `science` 等
- **verify_tls**：代理用 MITM HTTPS 证书（Clash/V2Ray 的 MITM 模式）时，`verify=False` 避免 TLS 校验失败。默认 `True`（安全）
- **`httpx.Client`（非 `httpx.get`）**：用 Client 是为了同时传 `proxy` + `verify` + `timeout`。`httpx.get` 不支持 `proxy` 参数

### 3. WS 降级通知

新建 `agent/tools/web_search_push.py`（~30 行），复用 `progress_push.py` 模式：

```python
async def push_search_fallback(session_id: str, backend: str, reason: str) -> None:
    """Best-effort WS push: 搜索后端降级通知。

    前端收到后显示 toast/通知："Tavily 无法使用，已切换到 SearXNG"
    """
    session_id = (session_id or "").strip()
    if not session_id:
        return
    try:
        from runtime.session.relation_register import get_websocket_by_session_id
        websocket = get_websocket_by_session_id(session_id)
        if websocket is None:
            return
        payload = {
            "event": "search_backend_fallback",
            "session_id": session_id,
            "content": {
                "backend": backend,           # "searxng" — 当前使用的后端
                "reason": reason,             # "tavily_auth_failed" / "tavily_timeout" / ...
                "message": f"搜索后端已从 Tavily 降级到 SearXNG（{reason}）",
            },
        }
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.warning("Failed to push search_backend_fallback for session {}: {}", session_id, e)
```

前端收到 `search_backend_fallback` 帧时：
- 显示一个 toast 通知（非阻塞）："⚠️ Tavily 无法使用，已切换到 SearXNG"
- 可选：在搜索结果区域标注"via SearXNG"

### 4. 修改 `agent/tools/web_search.py`（~60 行改动）

#### 4a. 分支逻辑

```python
def build_web_search_tool():
    from langchain_core.tools import tool
    from agent.tools.web_search_searxng import searxng_available, searxng_search
    from agent.tools.web_search_push import push_search_fallback

    if tavily_available() and searxng_available():
        # 两个都有：用户可选，Tavily 优先时 fallback 到 SearXNG
        base = TavilySearch(tavily_api_key=tavily_api_key, max_results=5)
        base._arun = _make_arun_with_fallback(original_arun=base._arun)
        return base

    elif tavily_available():
        # 只有 Tavily（当前行为，无 fallback）
        base = TavilySearch(tavily_api_key=tavily_api_key, max_results=5)
        base._arun = _arun_with_retry(base._arun)  # 当前 retry wrapper
        return base

    elif searxng_available():
        # 只有 SearXNG（主力）
        @tool("web_search", args_schema=WebSearchSchema)
        async def web_search(query: str) -> str:
            """Search the web for information."""
            return await asyncio.to_thread(searxng_search, query)
        web_search.handle_tool_error = True
        web_search.metadata = {"idempotent": False}
        return web_search

    else:
        # 两个都没配 → stub（当前行为）
        ...
```

#### 4b. 带 fallback 的 retry wrapper

```python
def _make_arun_with_fallback(original_arun):
    """Tavily retry + SearXNG fallback + WS 通知。"""

    async def _arun_with_fallback(*args, **kwargs):
        session_id = kwargs.get("session_id", "")  # 从 InjectedState 注入
        preferred = _resolve_preferred_backend(session_id)

        # 用户选了 SearXNG 为主力 → 直接用 SearXNG
        if preferred == "searxng":
            query = kwargs.get("query") or (args[0] if args else "")
            return await asyncio.to_thread(searxng_search, query)

        # 用户选了 Tavily（默认）→ Tavily retry + fallback
        last_error = None
        fallback_reason = None

        for attempt in range(RETRY_MAX_ATTEMPTS):
            try:
                return await asyncio.wait_for(
                    original_arun(*args, **kwargs),
                    timeout=WEB_SEARCH_TIMEOUT,
                )
            except TimeoutError:
                last_error = f"timed out after {WEB_SEARCH_TIMEOUT}s"
                fallback_reason = "tavily_timeout"
                logger.warning("web_search attempt {}/{} {}", ...)
            except Exception as e:
                last_error = str(e)
                if _is_auth_error(e):
                    fallback_reason = "tavily_auth_failed"
                    logger.warning("Tavily auth failed, falling back to SearXNG")
                    break  # 不重试，直接 fallback
                fallback_reason = "tavily_error"
                logger.warning("web_search attempt {}/{} failed: {}", ...)

            if attempt < RETRY_MAX_ATTEMPTS - 1:
                await asyncio.sleep(_backoff_delay(attempt))

        # Tavily 全部失败 → SearXNG fallback + WS 通知
        if searxng_available():
            logger.info("web_search: falling back to SearXNG ({})", fallback_reason)
            # WS 通知前端
            await push_search_fallback(session_id, "searxng", fallback_reason)
            # 执行 SearXNG 搜索
            query = kwargs.get("query") or (args[0] if args else "")
            return await asyncio.to_thread(searxng_search, query)

        # SearXNG 也没配 → 返回错误（当前行为）
        return (
            f"Web search failed after {RETRY_MAX_ATTEMPTS} attempts. "
            f"Last error: {last_error}. "
            "Please try a more specific query or answer without web search."
        )

    return _arun_with_fallback


def _is_auth_error(exc: Exception) -> bool:
    """Tavily key 失效 → 跳过重试，直接 fallback。"""
    msg = str(exc).lower()
    return any(kw in msg for kw in ("401", "403", "unauthorized", "forbidden", "invalid api key"))
```

### 5. 导入时副作用修复

当前 `web_search.py:10` 在 import 时 `load_dotenv(override=True)` + 读 env。改为延迟读取（函数内 `os.getenv`），对齐设计模式报告 S4。

## Fallback 触发条件

| 场景 | 行为 | WS 通知 |
|---|---|---|
| 用户选 Tavily，Tavily 正常 | Tavily 结果 | 无 |
| 用户选 Tavily，Tavily 401/403 | 不重试，立即 fallback SearXNG | ✅ "tavily_auth_failed" |
| 用户选 Tavily，Tavily 超时×3 | 重试完 → fallback SearXNG | ✅ "tavily_timeout" |
| 用户选 Tavily，Tavily 失败 + SearXNG 没配 | 返回错误字符串 | 无 |
| 用户选 SearXNG | 直接用 SearXNG，不 fallback | 无 |
| 用户选 SearXNG，但 SearXNG 没配 | `_resolve_preferred_backend` 兜底回 Tavily | 无 |
| 两个都没配 | stub "unavailable" | 无 |

## WS 帧格式

```json
{
  "event": "search_backend_fallback",
  "session_id": "sess_abc123",
  "content": {
    "backend": "searxng",
    "reason": "tavily_auth_failed",
    "message": "搜索后端已从 Tavily 降级到 SearXNG（tavily_auth_failed）"
  }
}
```

前端收到后：
- toast 通知（非阻塞）："⚠️ Tavily 无法使用，已切换到 SearXNG"
- 搜索结果区域可选标注"via SearXNG"
- 不中断当前 turn

## 改动清单

| 文件 | 角色 | 行数 |
|---|---|---|
| `agent/tools/web_search_searxng.py` | 新建：SearXNG 搜索函数 | ~80 |
| `agent/tools/web_search_push.py` | 新建：WS 降级通知（复用 progress_push 模式） | ~30 |
| `agent/tools/web_search.py` | 修改：分支逻辑 + fallback wrapper + WS 通知 | ~60 改动 |
| `runtime/session/state_keys.py` | 修改：加 `SEARCH_BACKEND` | ~2 |
| `config/features/infra_side/web_search.py` | 新建：`WebSearchConfig` TypedDict | ~10 |
| `config/features/__init__.py` | 修改：re-export | ~2 |
| `server/trigger/http/session_settings.py` | 修改：加 `GET/PUT /sessions/search_backend` | ~40 |
| `server/trigger/http/__init__.py` 或路由注册 | 修改：注册新路由 | ~4 |
| `tests/agent/tools/test_web_search_searxng.py` | 新建：SearXNG 单元测试（含 proxy/engines/TLS） | ~120 |
| `tests/agent/tools/test_web_search_fallback.py` | 新建：fallback + WS 通知测试 | ~80 |
| `tests/server/test_session_search_backend.py` | 新建：HTTP 端点测试 | ~40 |
| 前端"模型联网"入口 | 新建：菜单条目 + 右侧 sidebar tab（3 个 section） | 前端另计 |
| 前端 WS handler | 修改：处理 `search_backend_fallback` 帧（角标 + banner） | 前端另计 |

**后端合计：~470 行**（新建 230 + 修改 140 + 测试 240）

## 验收

- 菜单有"模型联网"入口，点击打开右侧 sidebar tab
- tab 内三个 section：搜索引擎（单选）/ SearXNG 连接（展开式表单）/ Tavily 状态（只读）
- 未配的后端选项灰禁；两个都没配 → "未配置任何搜索引擎"
- 选 Tavily + key 失效 → SearXNG 结果返回 + ChatBox 顶部 banner 通知
- 选 SearXNG → 直接用 SearXNG，不试 Tavily
- `PUT /sessions/search_backend` 写入 StateRegister，`GET` 读回
- WS 帧 `search_backend_fallback` 在降级时推送，best-effort 不中断 turn
- 两层都没配 → stub "unavailable"（当前行为，无回归）
- banner 通知非阻塞，不中断当前 turn
- SearXNG 连接配置：proxy / engines / categories / verify_tls 在"模型联网"tab 的 SearXNG section 可配
- 配了 proxy → httpx 请求走代理；配了 engines → SearXNG 按指定引擎搜索
- 代理连接失败时错误消息含 proxy 地址提示（"via proxy http://..." 或 "direct connection"）
- `SEARXNG_VERIFY_TLS=false` → httpx `verify=False`（MITM 代理证书场景）

## 不做的事

- **不做 provider 注册表/插件体系**——只有两个后端，hardcode 两层 fallback
- **不做 SearXNG extract（URL 抓取）**——SearXNG 只搜索
- **不做 SearXNG 分页**——`pageno=1` 够用
- **SearXNG 失败不 fallback 回 Tavily**——自托管场景下 Tavily 可能也没配；避免 ping-pong

## 关键文件路径

| 文件 | 角色 |
|---|---|
| `agent/tools/web_search_searxng.py` | 新建：SearXNG 搜索 |
| `agent/tools/web_search_push.py` | 新建：WS 降级通知 |
| `agent/tools/web_search.py` | 修改：分支 + fallback + WS |
| `agent/tools/taskflow/progress_push.py` | 参考：WS 推送模式 |
| `runtime/session/state_keys.py` | 修改：加 `SEARCH_BACKEND` |
| `runtime/session/state_register.py` | 参考：state 读写 |
| `runtime/session/relation_register.py` | 参考：`get_websocket_by_session_id` |
| `server/trigger/http/session_settings.py` | 修改：加 search_backend 端点 |
| `config/features/infra_side/web_search.py` | 新建：配置 |
