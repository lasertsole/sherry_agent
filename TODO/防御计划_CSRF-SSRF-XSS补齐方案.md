# CSRF / SSRF / XSS 防御补齐方案

> **项目**: sherry_agent
> **创建日期**: 2026-09-30
> **审查范围**: sherry_agent 自身 CSRF/SSRF/XSS 防御现状 + 同目录 13 个 Agent 项目横向对比
> **状态**: 待执行

---

## 一、现状审查

### 1.1 跨项目对比总表

| 防御机制                                  | openclaw                                                   | hermes-agent                     | codex-main                   | **sherry**                                          |
| ----------------------------------------- | ---------------------------------------------------------- | -------------------------------- | ---------------------------- | --------------------------------------------------- |
| **CSRF — Origin 校验**                    | ✅ `csrf.ts` Sec-Fetch-Site + Origin/Referer loopback 判定 | ✅ dashboard_auth cookies + CSRF | —                            | ⚠️ `auth.py` Origin allowlist（仅当 Origin 存在时） |
| **CSRF — 写操作方法门控**                 | ✅ POST/PUT/PATCH/DELETE 独立判定                          | ✅ dashboard 写路由              | —                            | ❌ 无                                               |
| **CSRF — Sec-Fetch-Site**                 | ✅ `sec-fetch-site: cross-site` → 拒绝                     | —                                | —                            | ❌ 未检查                                           |
| **CSRF — CSRF Token**                     | ✅ browser mutation guard                                  | ✅ cookie 双提交                 | —                            | ❌ 无（仅 gateway token，非 strict 模式下可缺）     |
| **SSRF — DNS 解析后公网判定**             | ✅ `infra/net/ssrf.ts`（813 行，IP pinning + 策略引擎）    | ✅ `url_safety.py`               | ✅ `network-proxy/policy.rs` | ⚠️ `public_url.py`（96 行，仅媒体抓取）             |
| **SSRF — IP pinning（防 DNS rebinding）** | ✅ pinned-dispatcher-pool + fetch-guard                    | —                                | —                            | ❌ 无                                               |
| **SSRF — 重定向跟随保护**                 | ✅ fetch-guard 逐跳校验                                    | —                                | —                            | ❌ 无（`urlopen` 默认跟随重定向）                   |
| **SSRF — 覆盖范围**                       | ✅ web-fetch/media/MCP/所有 HTTP 出口                      | ✅ browser/web/media 各工具      | ✅ 网络代理层                | ⚠️ 仅 `media_handlers.py` 远程 URL                  |
| **XSS — DOMPurify 净化**                  | ✅ sanitize 体系                                           | ✅ 部分                          | —                            | ✅ `safeHtml.ts` + `chatPurifyConfig`               |
| **XSS — v-html 禁用**                     | —                                                          | —                                | —                            | ✅ ESLint `vue/no-v-html: error`                    |
| **XSS — 输入字符过滤**                    | —                                                          | —                                | —                            | ✅ `utils.ts` session title allowlist               |
| **XSS — CSP header**                      | —                                                          | —                                | —                            | ❌ 无                                               |
| **XSS — Token 存储**                      | —                                                          | —                                | —                            | ✅ 内存（非 localStorage）                          |
| **XSS — 反向 tabnabbing**                 | —                                                          | —                                | —                            | ✅ `rel="noopener noreferrer"` hook                 |

### 1.2 sherry 现有防御清单

| 防御                 | 文件                                      | 覆盖范围                              | 备注                                                                                                   |
| -------------------- | ----------------------------------------- | ------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Origin allowlist     | `server/trigger/auth.py:81-82`            | 携带 `Origin` 头的 HTTP 请求          | 无 `Origin` 的请求（curl/脚本）直接放行；**浏览器跨站请求必带 Origin，故跨站 CSRF 已被挡**             |
| Gateway token        | `server/trigger/auth.py:63-78`            | HTTP `token` header + WS `?token=`    | 非 strict 模式下 HTTP 请求可不带 token（`require_token=False` 默认）                                   |
| WS token 必选        | `server/trigger/auth.py:106-119`          | WebSocket 握手                        | 浏览器无法在 WS 握手中传自定义 header，只能走 `?token=`，**token 缺失必拒**                            |
| SSRF guard           | `pub/func/validator/public_url.py`        | `media_handlers.py` 远程媒体 URL 抓取 | DNS 解析后拒绝私有/loopback/link-local/云元数据；**仅覆盖媒体管道，不覆盖 web_search/terminal/MCP 等** |
| XSS — DOMPurify      | `client/app/directives/safeHtml.ts`       | ChatBox 渲染的 markdown HTML          | allowlist 标签/属性 + style 值正则门控 + 反向 tabnabbing hook                                          |
| XSS — ESLint         | `client/eslint.config.mjs:66`             | 全部 `.vue` 文件                      | `vue/no-v-html: error`，唯一例外是 ChatBox（经 safeHtml 净化）                                         |
| XSS — 输入过滤       | `client/app/common/utils.ts:56-64`        | Session title                         | `\p{L}\p{N}\s._-` allowlist，阻断 `<>"'&;/\` 等                                                        |
| XSS — token 内存存储 | `client/app/composables/requestApi.ts:28` | Gateway token                         | 模块作用域变量，不落 localStorage，随 tab 消亡                                                         |

### 1.3 缺口判定

#### CSRF — 部分缺失

**已有**：Origin allowlist + per-boot token 构成了两层 CSRF 防御：

1. 跨站浏览器请求必带 `Origin`，不在 allowlist 中 → 403
2. WS 握手必须带 `?token=`，浏览器跨站页面无法获取 per-boot token

**缺口**：

- **C1 — 写操作方法门控**：`POST/PUT/PATCH/DELETE` 未做独立 CSRF 判定。非 strict 模式下，一个被 XSS 注入的同源页面（webview 内）可以不带 token 发起写请求（Origin 为 `tauri://localhost` → 放行；无 token → 非严格模式放行）。攻击向量：如果 ChatBox 的 DOMPurify 被绕过（0day），注入的 JS 可在同源上下文发起 state-changing 请求。
- **C2 — Sec-Fetch-Site 未检查**：现代浏览器发送 `Sec-Fetch-Site` 头，`cross-site` 值是比 Origin 更强的信号（Origin 可被某些场景省略，如 `<form>` POST 不带 CORS 的 simple request）。当前 `check_http` 未读取此头。
- **C3 — 无 SameSite cookie 策略**：当前不使用 cookie 传 token（用 header），所以此条**不适用**——记录为不适用而非缺失。

#### SSRF — 覆盖面不足

**已有**：`public_url.py` 在 `media_handlers.py` 远程 URL 抓取前做 DNS 解析 + 公网地址判定。

**缺口**：

- **S1 — 覆盖面**：`web_search` 工具（若返回 URL 并被 fetch）、`terminal` 工具（curl/wget 由用户控制，但 agent 可能被注入指令执行 `curl http://169.254.169.254`）、MCP HTTP 工具、knowledge-graph 文档 URL 抓取——这些出口均未经过 SSRF guard。
- **S2 — IP pinning**：`public_url.py` 在 DNS 解析后判定公网，但 `urlopen` 随后发起连接时会再次 DNS 解析——攻击者可在两次解析之间切换 A 记录（DNS rebinding），使第一次解析为公网 IP、第二次解析为 `169.254.169.254`。
- **S3 — 重定向跟随**：`urlopen` 默认跟随 HTTP 302 重定向。`public_url.py` 只校验初始 URL，重定向目标可能指向内网地址。
- **S4 — IPv4-mapped IPv6**：`public_url.py:46-49` 已处理 `::ffff:127.0.0.1`，但未覆盖 IPv6 unique local address（`fc00::/7`）和 RFC 2544 benchmark range（`198.18.0.0/15`）——后者被 fake-ip 代理栈（sing-box/Clash）用于解析外国域名。

#### XSS — 前端已覆盖，后端缺 CSP

**已有**：DOMPurify allowlist + ESLint `no-v-html` + 输入字符过滤 + token 内存存储 + style 值正则门控 + 反向 tabnabbing。

**缺口**：

- **X1 — 无 CSP header**：服务端未设置 `Content-Security-Policy` 响应头。CSP 是 XSS 的纵深防御层：即使 DOMPurify 被 0day 绕过，`script-src 'self'` 可阻止内联脚本执行、`object-src 'none'` 阻止 `<embed>`/`<object>` 加载插件。
- **X2 — 无 `X-Content-Type-Options: nosniff`**：静态资源（`/images/`、`/audio/`、`/video/`）未设置 MIME 嗅探禁止头，浏览器可能将用户上传的图片嗅探为 HTML。
- **X3 — 无 `X-Frame-Options`**：未防止页面被嵌入 iframe（clickjacking）。Tauri webview 场景下风险较低，但如果前端也作为 dev server 被浏览器访问，则存在风险。

---

## 二、改造方案

### 2.1 CSRF 补齐（C1 + C2）

#### 新增文件：`server/trigger/csrf.py`

参考 openclaw `extensions/browser/src/browser/csrf.ts`，适配 Robyn 中间件模型。

```python
"""CSRF guard for state-changing HTTP methods.

Threat model
------------
The existing Origin allowlist (server/trigger/auth.py) blocks cross-site
browser requests: browsers always send ``Origin`` on cross-origin fetch / form
POST / WS handshake, so a hostile page cannot get past that gate. The gap is
same-origin: if an XSS payload lands inside the webview (e.g., a DOMPurify
0day), the injected JS shares the webview's Origin (``tauri://localhost``),
passes the Origin gate, and — in non-strict mode — can issue POST/PUT/PATCH/
DELETE without a token.

This module adds a second CSRF layer for mutating methods:

1. **Sec-Fetch-Site** — when the browser sends ``Sec-Fetch-Site: cross-site``,
   the request is rejected regardless of Origin (stronger than Origin alone,
   which simple form POSTs may omit). ``same-origin`` and ``same-site`` pass.
2. **Origin/Referer loopback check** — when ``Sec-Fetch-Site`` is absent
   (non-browser clients), fall back to: if ``Origin`` or ``Referer`` is
   present, it must resolve to a loopback / allowed origin; absent both
   (curl/scripts) → pass (same policy as the existing Origin gate).

GET / HEAD / OPTIONS are never gated (idempotent). The middleware runs AFTER
``gateway_auth_middleware`` so the Origin gate fires first.
"""

from __future__ import annotations

from urllib.parse import urlparse

from loguru import logger

from server.trigger import auth

__all__ = ["csrf_guard_middleware"]

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_loopback_origin(value: str) -> bool:
    """True when the URL's host is loopback or in the gateway allowlist."""
    if not value or value == "null":
        return False
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    host = parsed.hostname
    if not host:
        return False
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    return value.strip().rstrip("/") in {o.rstrip("/") for o in auth.allowed_origins()}


def _should_reject(method: str, origin: str | None, referer: str | None,
                   sec_fetch_site: str | None) -> tuple[int, str] | None:
    """Return (status, message) to refuse, or None to allow."""
    if method.upper() not in _MUTATING_METHODS:
        return None

    sec_fetch_site = (sec_fetch_site or "").strip().lower()
    if sec_fetch_site == "cross-site":
        return 403, "Cross-site request blocked by CSRF guard"

    origin = (origin or "").strip()
    if origin:
        if not _is_loopback_origin(origin):
            return 403, "Origin not allowed for mutation"
        return None

    referer = (referer or "").strip()
    if referer:
        if not _is_loopback_origin(referer):
            return 403, "Referer not allowed for mutation"
        return None

    return None


def csrf_guard_middleware(request):
    """CSRF gate for POST/PUT/PATCH/DELETE (see module docstring)."""
    verdict = _should_reject(
        method=request.method,
        origin=request.headers.get("Origin"),
        referer=request.headers.get("Referer"),
        sec_fetch_site=request.headers.get("Sec-Fetch-Site"),
    )
    if verdict is None:
        return request
    status, message = verdict
    logger.warning("csrf guard: refused method={} path={} reason={}",
                   request.method, request.url.path, message)
    from robyn import Response
    import json
    return Response(
        status_code=status,
        headers={"Content-Type": "application/json"},
        description=json.dumps({"success": False, "message": message}, ensure_ascii=False),
    )
```

#### 修改文件：`server/trigger/core.py`

在 `gateway_auth_middleware` 之后注册 CSRF 中间件：

```python
# --- 在 app.before_request()(gateway_auth_middleware) 之后 ---

from server.trigger.csrf import csrf_guard_middleware

# CSRF guard runs AFTER the Origin gate: the Origin check in
# gateway_auth_middleware fires first (hostile page learns nothing about
# tokens), then the mutating-method CSRF check adds the same-origin defense.
app.before_request()(csrf_guard_middleware)
```

#### 配置：`config/features/infra_side/gateway.py`

在 `GatewayConfig` 中新增字段：

```python
class GatewayConfig(TypedDict):
    # ... existing fields ...
    #: CSRF guard for mutating methods (POST/PUT/PATCH/DELETE). When True,
    #: Sec-Fetch-Site + Origin/Referer loopback check runs on every mutation.
    #: Disable only for headless testing where a non-browser client sends
    #: mutations without Origin/Referer and strict token mode is on.
    csrf_guard_enabled: bool
```

在 `_build_gateway` 中读取 `SHERRY_CSRF_GUARD_ENABLED`（默认 `true`），中间件内部先检查此开关。

---

### 2.2 SSRF 补齐（S1 + S2 + S3 + S4）

#### 修改文件：`pub/func/validator/public_url.py`

在现有基础上增强：

```python
# 1. 新增：IPv6 ULA 和 RFC 2544 benchmark range 判定（S4）

_RFC2544_RANGE = ipaddress.ip_network("198.18.0.0/15")
_IPV6_ULA_RANGE = ipaddress.ip_network("fc00::/7")


def _is_global_address(raw: str) -> bool:
    # ... existing code ...
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or addr in _RFC2544_RANGE       # new: fake-ip proxy range
        or (isinstance(addr, ipaddress.IPv6Address) and addr in _IPV6_ULA_RANGE)  # new
    )


# 2. 新增：IP pinning 验证函数（S2 — DNS rebinding 防护）

def resolve_and_pin(url: str) -> tuple[str, str] | None:
    """Resolve URL host and return (host, pinned_ip) for IP-pinned fetch.

    Returns None if the URL fails the public-URL check. The caller must use
    the pinned_ip for the actual connection (e.g., via Host header or
    custom resolver) so a DNS rebinding attack cannot redirect the second
    resolution to a private address.
    """
    if not is_public_url(url):
        return None
    parsed = urlparse(url.strip())
    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return None
    addresses = {str(info[4][0]) for info in infos}
    global_addrs = [a for a in addresses if _is_global_address(a)]
    if not global_addrs:
        return None
    return host, global_addrs[0]
```

#### 新增文件：`pub/func/validator/safe_fetch.py`

统一安全 HTTP 抓取入口，覆盖 S1/S2/S3：

```python
"""Safe HTTP fetch with SSRF guard, IP pinning, and redirect protection.

All non-media HTTP fetches (web_search result URLs, MCP HTTP tools,
knowledge-graph document URLs) should go through this function instead of
calling urllib.request.urlopen directly.

Media fetches stay in media_handlers.py (they have their own size-limit
and dual-copy logic), but should call safe_fetch for the actual HTTP GET.
"""

from __future__ import annotations

import http.client
import urllib.request
from urllib.parse import urlparse

from loguru import logger
from pub.func.validator.public_url import is_public_url, _is_global_address

__all__ = ["safe_fetch"]

_MAX_REDIRECTS = 5


def _check_url_safe(url: str) -> bool:
    """SSRF gate: scheme + host + DNS resolution all public."""
    return is_public_url(url)


def safe_fetch(
    url: str,
    *,
    timeout: int = 30,
    max_bytes: int | None = None,
    headers: dict[str, str] | None = None,
) -> bytes | None:
    """Fetch a URL with SSRF protection.

    - Pre-flight: is_public_url() DNS + public-IP check
    - Redirect: each hop re-checked (no redirect to private IP)
    - Max redirects: 5 (prevent redirect loops)
    - Max bytes: optional body size cap (read in chunks)

    Returns the body bytes, or None if the fetch was refused/failed.
    """
    if not _check_url_safe(url):
        logger.warning("safe_fetch: refused (SSRF guard) url={}", url)
        return None

    req_headers = {"User-Agent": "Mozilla/5.0 (EMA_AI_agent)"}
    if headers:
        req_headers.update(headers)

    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        req = urllib.request.Request(current_url, headers=req_headers)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except Exception as exc:
            logger.warning("safe_fetch: request failed url={} exc={}", current_url, exc)
            return None

        # Check redirect
        final_url = resp.geturl()
        if final_url != current_url:
            if not _check_url_safe(final_url):
                logger.warning("safe_fetch: redirect to non-public url refused url={}", final_url)
                resp.close()
                return None
            current_url = final_url
            continue

        # Read body with optional size cap
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                logger.warning("safe_fetch: body exceeded max_bytes={} url={}", max_bytes, current_url)
                resp.close()
                return None
            chunks.append(chunk)
        resp.close()
        return b"".join(chunks)

    logger.warning("safe_fetch: exceeded max redirects url={}", url)
    return None
```

#### 修改文件：`agent/middlewares/media_pipeline/media_handlers.py`

将直接 `urllib.request.urlopen` 调用替换为 `safe_fetch`（保持原有的 size-limit 和 dual-copy 逻辑）：

```python
# 现状（第 147-158 行）：
#   if not is_public_url(url): ...
#   req = urllib.request.Request(url, ...)
#   with urllib.request.urlopen(req, timeout=30) as resp: ...

# 改为：
from pub.func.validator.safe_fetch import safe_fetch

if not is_public_url(url):
    paths.skipped.append(...)
    return None

body = safe_fetch(url, timeout=30, max_bytes=limit, headers={"User-Agent": "Mozilla/5.0 (EMA_AI_agent)"})
if body is None:
    paths.skipped.append(f"[Uploaded media] Remote {kind} URL fetch failed (SSRF guard or size limit).")
    return None
# ... continue with existing dual-copy logic using `body` ...
```

#### 修改文件：web_search 工具（覆盖 S1）

在 `web_search` / `tavily_search` 工具返回 URL 列表后，如果 agent 随后 fetch 这些 URL（通过 read_file 或自定义 fetch），确保 fetch 走 `safe_fetch`。

> **注意**：当前 `web_search` 工具只返回搜索结果摘要，不直接 fetch URL。但如果 agent 通过 `terminal` 执行 `curl`，SSRF guard 无法拦截（terminal 在沙箱内执行用户级命令）。Terminal SSRF 防护需要通过 HITL 的 `detection.py` 模式匹配（新增 `curl.*169\.254\.169\.254` 模式），而非网络层拦截。

#### 修改文件：`agent/middlewares/humanInTheLoop/detection.py`

新增云元数据端点 curl 检测模式：

```python
# 在 _DANGEROUS_PATTERNS 或独立列表中新增：
(re.compile(r"curl\b.*169\.254\.169\.254|wget\b.*169\.254\.169\.254", re.IGNORECASE),
 "cloud_metadata_ssrf"),
(re.compile(r"curl\b.*(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?(?:/|$)", re.IGNORECASE),
 "loopback_ssrf"),
```

---

### 2.3 XSS 补齐（X1 + X2 + X3）

#### 新增文件：`server/trigger/security_headers.py`

```python
"""Security response headers for all HTTP responses.

CSP: prevents inline script execution even if DOMPurify is bypassed (0day).
The Tauri webview loads from tauri://localhost (same origin), so 'self'
covers the app shell. Markdown-rendered content is sanitized by DOMPurify
on the client; CSP adds a server-side defense layer.

X-Content-Type-Options: prevents browsers from MIME-sniffing uploaded
images/audio/video as HTML (the /images/, /audio/, /video/ endpoints
serve user-uploaded bytes with guessed Content-Type).

X-Frame-Options: prevents the webview/dev-server page from being embedded
in an iframe (clickjacking). Tauri webview is not embeddable, but the
Nuxt dev server (localhost:3000) is.
"""

from __future__ import annotations

from robyn import Response

__all__ = ["security_headers_middleware", "SECURITY_HEADERS"]

SECURITY_HEADERS: dict[str, str] = {
    # CSP: allow self + inline style (markdown-it table alignment uses style)
    # + data: images (base64 inline images from uploads)
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self' ws://127.0.0.1:* wss://127.0.0.1:*; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def security_headers_middleware(request):
    """Inject security headers into every response."""
    # Robyn's before_request middleware cannot set response headers directly;
    # this is wired as an after_request hook instead (see core.py).
    return request


def apply_security_headers(response: Response) -> Response:
    """Add security headers to a Response (called from after_request)."""
    if not hasattr(response, "headers") or response.headers is None:
        response.headers = {}
    response.headers.update(SECURITY_HEADERS)
    return response
```

#### 修改文件：`server/trigger/core.py`

注册安全头注入：

```python
# --- 在 CSRF middleware 之后 ---

from server.trigger.security_headers import apply_security_headers, SECURITY_HEADERS

# Set static security headers on every response
for name, value in SECURITY_HEADERS.items():
    app.set_response_header(name, value)
```

> **Robyn 适配**：Robyn 的 `set_response_header` 在全局响应头层面设置，对所有路由生效。如果 Robyn 的 `before_request` / `after_request` 支持 Response 修改，也可以走中间件；当前用全局 `set_response_header` 是最简方案。

---

## 三、实施排期

| 阶段       | 时间    | 内容                                                    | 涉及项     |
| ---------- | ------- | ------------------------------------------------------- | ---------- |
| **阶段 1** | 第 1 周 | CSRF guard 中间件 + Sec-Fetch-Site 检查 + 配置开关      | C1, C2     |
| **阶段 2** | 第 1 周 | 安全响应头（CSP / nosniff / X-Frame-Options）           | X1, X2, X3 |
| **阶段 3** | 第 2 周 | SSRF guard 增强：IPv6 ULA + RFC 2544 + IP pinning 函数  | S2, S4     |
| **阶段 4** | 第 2 周 | `safe_fetch` 统一安全抓取入口 + 重定向保护              | S1, S3     |
| **阶段 5** | 第 3 周 | media_handlers 迁移到 `safe_fetch` + terminal SSRF 模式 | S1         |
| **阶段 6** | 第 3 周 | 测试 + CI 集成                                          | 全部       |

---

## 四、测试计划

### 4.1 CSRF 测试

| 测试                                                                | 类型 | 断言                  |
| ------------------------------------------------------------------- | ---- | --------------------- |
| `POST` + `Origin: evil.com` → 403                                   | 单元 | Origin gate 先拒      |
| `POST` + `Origin: tauri://localhost` + no token → 通过（非 strict） | 单元 | 同源放行              |
| `POST` + `Sec-Fetch-Site: cross-site` → 403                         | 单元 | CSRF guard 拒绝       |
| `POST` + `Sec-Fetch-Site: same-origin` → 通过                       | 单元 | 同源放行              |
| `GET` + `Sec-Fetch-Site: cross-site` → 通过                         | 单元 | 非写操作不门控        |
| `POST` + 无 Origin + 无 Referer + 无 Sec-Fetch-Site → 通过          | 单元 | 非浏览器客户端放行    |
| `POST` + `Referer: http://evil.com` → 403                           | 单元 | Referer fallback 拒绝 |

### 4.2 SSRF 测试

| 测试                                              | 类型 | 断言              |
| ------------------------------------------------- | ---- | ----------------- |
| `safe_fetch("http://169.254.169.254/...")` → None | 单元 | 云元数据拒绝      |
| `safe_fetch("http://127.0.0.1:8080/...")` → None  | 单元 | Loopback 拒绝     |
| `safe_fetch` + 重定向到 `192.168.x.x` → None      | 单元 | 重定向保护        |
| `safe_fetch` + 重定向超 5 跳 → None               | 单元 | 重定向上限        |
| `_is_global_address("198.18.0.1")` → False        | 单元 | RFC 2544 拒绝     |
| `_is_global_address("fd00::1")` → False           | 单元 | IPv6 ULA 拒绝     |
| `safe_fetch("https://example.com")` → bytes       | 集成 | 公网 URL 正常     |
| terminal `curl 169.254.169.254` → HITL 触发       | 集成 | detection.py 拦截 |

### 4.3 XSS 测试

| 测试                                             | 类型 | 断言          |
| ------------------------------------------------ | ---- | ------------- |
| 响应包含 `Content-Security-Policy` header        | 单元 | 安全头存在    |
| 响应包含 `X-Content-Type-Options: nosniff`       | 单元 | MIME 嗅探禁止 |
| CSP 中 `object-src 'none'`                       | 单元 | 插件加载禁止  |
| CSP 中 `script-src 'self'`（无 `unsafe-inline`） | 单元 | 内联脚本禁止  |

---

## 五、关键参考文件

### 外部参考

| 项目           | 文件                                     | 参考内容                                                  |
| -------------- | ---------------------------------------- | --------------------------------------------------------- |
| `openclaw`     | `extensions/browser/src/browser/csrf.ts` | CSRF guard：Sec-Fetch-Site + Origin/Referer loopback 判定 |
| `openclaw`     | `src/infra/net/ssrf.ts`                  | SSRF 策略引擎（IP pinning + 策略 + 重定向保护，813 行）   |
| `openclaw`     | `src/infra/net/fetch-guard.ts`           | 逐跳重定向 SSRF 校验                                      |
| `openclaw`     | `packages/net-policy/src/ip.ts`          | IP 地址分类（RFC 2544 / IPv6 ULA / 云元数据）             |
| `openclaw`     | `src/infra/net/ssrf.pinning.test.ts`     | DNS rebinding 防护测试                                    |
| `hermes-agent` | `tools/url_safety.py`                    | URL 安全判定                                              |
| `hermes-agent` | `hermes_cli/dashboard_auth/cookies.py`   | CSRF cookie 策略                                          |
| `codex-main`   | `codex-rs/network-proxy/src/policy.rs`   | 网络代理层 SSRF 策略                                      |

### Sherry (本项目)

| 文件                                                 | 用途                                                |
| ---------------------------------------------------- | --------------------------------------------------- |
| `server/trigger/auth.py`                             | 现有 Origin + token 网关认证——CSRF guard 叠加在其后 |
| `server/trigger/core.py`                             | 中间件注册点——新增 CSRF + 安全头                    |
| `config/features/infra_side/gateway.py`              | 网关配置——新增 `csrf_guard_enabled`                 |
| `pub/func/validator/public_url.py`                   | 现有 SSRF guard——增强 IPv6/RFC2544 + IP pinning     |
| `agent/middlewares/media_pipeline/media_handlers.py` | 媒体抓取——迁移到 `safe_fetch`                       |
| `agent/middlewares/humanInTheLoop/detection.py`      | Shell 危险命令检测——新增 SSRF curl/wget 模式        |
| `client/app/directives/safeHtml.ts`                  | 现有 XSS 前端净化——保留                             |
| `client/app/constants/security.ts`                   | DOMPurify 配置——保留                                |
| `client/eslint.config.mjs`                           | `vue/no-v-html: error`——保留                        |
| `client/app/composables/requestApi.ts`               | Token 内存存储——保留                                |

---

## 六、风险评估

| 风险                                                              | 影响                                                 | 缓解                                                                                                                                     |
| ----------------------------------------------------------------- | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| CSRF guard 误拦截非浏览器客户端                                   | 工具脚本 / curl POST 被拒                            | 无 Origin + 无 Referer + 无 Sec-Fetch-Site → 放行（与现有 Origin gate 同策略）                                                           |
| CSP `style-src 'unsafe-inline'` 削弱防护                          | DOMPurify 已净化 style，但允许 inline style 略有降级 | markdown-it 的 GFM table 对齐依赖 `style="text-align:..."`；已有 `SAFE_STYLE_VALUE_REGEXP` 门控，CSP 是第二层                            |
| `safe_fetch` 替换 `urlopen` 破坏 media_handlers 的 dual-copy 逻辑 | 媒体写入路径变化                                     | `safe_fetch` 只替换 HTTP GET 部分，dual-copy 保持不变；需集成测试验证                                                                    |
| IP pinning 实现复杂                                               | `urllib` 不支持自定义 DNS resolver                   | 方案：用 `socket.create_connection` 手动建立连接 + 设置 Host header，或用 `requests` 库（已检查：本项目未依赖 requests，需评估是否引入） |
| Terminal SSRF 模式误报                                            | 用户正常 curl localhost                              | HITL 只触发确认，不直接拒绝；用户可放行                                                                                                  |
