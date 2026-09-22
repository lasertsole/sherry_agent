# 代码检索框架 — 补充计划

> 基于 oh-my-openagent-dev 五层 LSP 安装基础设施和 ast-grep MCP 的深度调研，
> 补充 [`CODE_INTEL_SUBAGENT_PLAN.md`](CODE_INTEL_SUBAGENT_PLAN.md) 的五个缺口：
> LSP 二进制发现与自动安装、ast-grep 结构化搜索、LSP 语言/工具扩展、
> 文件事件自动同步、外部代码检索 subagent。
>
> **本文件是对原计划的修订与补充，不替代原计划。**
> 实施时原计划的 Phase 1 不变；Phase 2 由本文件 Phase 2S 替代；
> 新增 Phase 1A（必选，与 Phase 1 同级）、Phase 4 为独立阶段。
>
> **ast-grep 是核心必选组件**（对标 oh-my-openagent，在该项目中 ast-grep
> 与 LSP daemon 并列为无条件注册的核心组件，非 opt-in）。

---

## 目录

1. [缺口总览](#缺口总览)
2. [Phase 1A — ast-grep 结构化搜索（必选，与 Phase 1 同级）](#phase-1a--ast-grep-结构化搜索必选与-phase-1-同级)
3. [Phase 2S — LSP 二进制发现与自动安装（替代原 Phase 2）](#phase-2s--lsp-二进制发现与自动安装替代原-phase-2)
4. [Phase 2X — LSP 语言与工具扩展](#phase-2x--lsp-语言与工具扩展)
5. [Phase 4 — 外部代码检索 subagent](#phase-4--外部代码检索-subagent)
6. [Phase 5 — 文件事件自动同步（可选）](#phase-5--文件事件自动同步可选)
7. [修订后的总工期](#修订后的总工期)
8. [修改文件清单（新增）](#修改文件清单新增)
9. [测试计划（补充）](#测试计划补充)
10. [安全清单（补充）](#安全清单补充)

---

## 缺口总览

| #   | 缺口                                                         | oh-my-openagent 对标                                                    | 本计划阶段       |
| --- | ------------------------------------------------------------ | ----------------------------------------------------------------------- | ---------------- |
| 1   | LSP 二进制发现（repo-local → PATH → 安装提示）               | `server-installation.ts` (218 行)                                       | Phase 2S         |
| 2   | LSP 自动安装 + 安装提示 + 用户决策                           | `server-definitions.ts` + `install-decision.ts`                         | Phase 2S         |
| 3   | LSP fallback 策略（缺失时降级到 tree-sitter / search_files） | `server-resolution.ts` not_installed 状态                               | Phase 2S         |
| 4   | ast-grep 结构化搜索/重写                                     | `ast-grep-mcp/` (25 语言, 5 级 strictness)                              | Phase 1A（必选） |
| 5   | LSP 语言覆盖（4 → 12+）                                      | `BUILTIN_SERVERS` (40+ 语言)                                            | Phase 2X         |
| 6   | LSP 工具覆盖（4 → 8）                                        | symbols/goto-def/refs/rename/diagnostics/format/status/install-decision | Phase 2X         |
| 7   | 外部代码检索（GitHub/npm/docs）                              | `librarian` subagent                                                    | Phase 4          |
| 8   | 文件事件自动同步                                             | CodeGraph 2s debounce 文件监听                                          | Phase 5 (可选)   |

---

## Phase 1A — ast-grep 结构化搜索（必选，与 Phase 1 同级）

> **前置条件：subagent 功能角色分工（已落地）。**
> **定位：** 与 tree-sitter 索引（Phase 1）并列的核心代码检索层。
> tree-sitter 做离线索引（按符号名查询），ast-grep 做即时结构化搜索
> （"找到所有 `def $FUNC($$$):` 形式的函数定义"）。
>
> **对标依据：** oh-my-openagent 中 ast-grep 是无条件注册的核心组件
> （`component-list.ts:34` 硬编码 `createAstGrepComponent()`），
> MCP 服务器 `enabled: true, lifecycle: "lazy"`，与 LSP daemon 并列。
> 本计划将其定位为**必选阶段**，与 Phase 1 同级，不可跳过。

### 为什么不用 tree-sitter 查询代替 ast-grep

tree-sitter query DSL 面向**提取**（从已知文件提取符号），
ast-grep 面向**搜索**（跨文件树匹配 AST 模式 + 元变量捕获）。
两者互补：tree-sitter 做离线索引，ast-grep 做即时结构化搜索。

### 为什么不用 pip 包 `ast-grep-python` 代替直接二进制 provision

oh-my-openagent 不依赖任何 pip/npm 包捆绑的二进制，而是：

1. 从 GitHub releases 下载 pinned 版本（0.43.0）的预编译二进制
2. SHA-256 校验下载内容
3. 写入 `~/.omo/runtime/ast-grep/<platform-arch>/sg`
4. 每次使用前跑 `--version` 探针验证二进制可用

本计划采用相同策略，不依赖 `ast-grep-python` pip 包（该包捆绑的二进制
可能版本滞后、平台覆盖不全、且无法 pin 版本）。

### 依赖

```toml
# pyproject.toml — 不新增 pip 依赖
# ast-grep 二进制通过 provisioner 从 GitHub releases 下载，SHA-256 验证
```

### 二进制发现与自动 provision（5 层）

```
┌─ ast-grep 二进制发现层 ────────────────────────────────────────────────┐
│                                                                        │
│  resolve_sg_binary() — 5 层探测，每层跑 --version 探针                   │
│  ├─ 1. 环境变量覆盖 (SHERRY_SG_PATH)                                   │
│  ├─ 2. sherry runtime 目录 (~/.sherry/runtime/ast-grep/<slug>/sg)      │
│  ├─ 3. skill bin cache (skills/ast-grep/bin/sg)                         │
│  ├─ 4. PATH 查找 (ast-grep / sg 命令，含 Windows PATHEXT)              │
│  └─ 5. Homebrew / Linuxbrew 前缀 (/opt/homebrew/bin, /usr/local/bin)   │
│                                                                        │
│  所有层均未命中 → provision_sg_binary()                                │
│  ├─ 从 GitHub releases 下载 pinned 版本 (0.43.0)                       │
│  ├─ SHA-256 校验（6 平台 × 校验值硬编码）                              │
│  ├─ 解压 ZIP → 提取 sg/ast-grep 二进制                                 │
│  └─ 写入 ~/.sherry/runtime/ast-grep/<slug>/sg (chmod 755)              │
│                                                                        │
│  安装脚本 (install.sh / install.ps1) — 7 路 fallback                   │
│  brew → npm → cargo → pip → nix → mise → GitHub tarball               │
└────────────────────────────────────────────────────────────────────────┘
```

### 新增文件（7 个）

#### 1. `config/features/agent_side/ast_grep.py`

```python
"""ast-grep structural search configuration."""

from typing import TypedDict


class AstGrepConfig(TypedDict):
    """Configuration for ast-grep structural search tools."""
    ast_grep_max_matches: int
    ast_grep_max_pattern_bytes: int
    ast_grep_timeout_ms: int
    ast_grep_max_paths: int
    ast_grep_supported_languages: list[str]
    ast_grep_strictness_default: str  # cst | smart | ast | relaxed | signature
    # ── 二进制 provision ──
    ast_grep_pinned_version: str
    ast_grep_runtime_dir: str  # ~/.sherry/runtime/ast-grep
    ast_grep_path_env_key: str  # SHERRY_SG_PATH
    ast_grep_provision_timeout_s: float
    ast_grep_version_probe_timeout_ms: int
    ast_grep_release_assets: dict  # slug → {url, sha256}


AST_GREP: AstGrepConfig = {
    "ast_grep_max_matches": 50,
    "ast_grep_max_pattern_bytes": 16384,
    "ast_grep_timeout_ms": 30000,
    "ast_grep_max_paths": 64,
    "ast_grep_supported_languages": [
        "python", "typescript", "tsx", "javascript", "rust", "go",
        "c", "cpp", "csharp", "java", "ruby", "html", "css", "json",
        "yaml", "bash", "lua", "swift", "kotlin", "scala",
        "php", "elixir", "haskell", "solidity",
    ],
    "ast_grep_strictness_default": "smart",
    # ── 二进制 provision ──
    "ast_grep_pinned_version": "0.43.0",
    "ast_grep_runtime_dir": "",  # 运行时填充为 ~/.sherry/runtime/ast-grep
    "ast_grep_path_env_key": "SHERRY_SG_PATH",
    "ast_grep_provision_timeout_s": 60.0,
    "ast_grep_version_probe_timeout_ms": 5000,
    # GitHub release assets — 6 平台 × SHA-256（参考 oh-my-openagent sg-manifest.ts）
    "ast_grep_release_assets": {
        "darwin-arm64": {
            "url": "https://github.com/ast-grep/ast-grep/releases/download/0.43.0/app-aarch64-apple-darwin.zip",
            "sha256": "8c847d0a29aa4b3101b3361e0b3ee7fb53c7e497adc9ed1afc9615538cd40782",
        },
        "darwin-x64": {
            "url": "https://github.com/ast-grep/ast-grep/releases/download/0.43.0/app-x86_64-apple-darwin.zip",
            "sha256": "6d703090b106747b2f56086b6ccc7e798fe78bcae70257aa20519b220153555b",
        },
        "linux-arm64": {
            "url": "https://github.com/ast-grep/ast-grep/releases/download/0.43.0/app-aarch64-unknown-linux-gnu.zip",
            "sha256": "e706846148493967f3ab8011334817edd86ce5acbec10718b2a7b40799c640ff",
        },
        "linux-x64": {
            "url": "https://github.com/ast-grep/ast-grep/releases/download/0.43.0/app-x86_64-unknown-linux-gnu.zip",
            "sha256": "a26253a9c821d935f7e383e40f0de7c2ca62a4121de1f73a6d81ec32eae631e0",
        },
        "win32-arm64": {
            "url": "https://github.com/ast-grep/ast-grep/releases/download/0.43.0/app-aarch64-pc-windows-msvc.zip",
            "sha256": "a519fdd90324bf6858fde2d3feb2b862d67b834dc11af8f5b6c2c8143ab6a6c5",
        },
        "win32-x64": {
            "url": "https://github.com/ast-grep/ast-grep/releases/download/0.43.0/app-x86_64-pc-windows-msvc.zip",
            "sha256": "a4febbc8c48671e5729d85e29e4ebe5a051b7250d19545bca18e725ccf40ef61",
        },
    },
}
```

#### 2. `agent/tools/code_intel/ast_grep/__init__.py`

```python
"""ast-grep structural search — all subagents (core component)."""

from .runner import build_ast_grep_tools

__all__ = ["build_ast_grep_tools"]
```

#### 3. `agent/tools/code_intel/ast_grep/resolver.py` — 5 层二进制发现

```python
"""ast-grep binary resolution — 5-tier discovery with --version probe.

Probing order (mirrors oh-my-openagent sg-resolver.ts + sg-candidates.ts):
  1. Env override (SHERRY_SG_PATH)
  2. sherry runtime (~/.sherry/runtime/ast-grep/<slug>/sg)
  3. skill bin cache (skills/ast-grep/bin/sg)
  4. PATH lookup (ast-grep / sg, with Windows PATHEXT)
  5. Homebrew / Linuxbrew prefixes

Every candidate must be a non-empty regular file AND pass a 5s --version
probe whose output contains 'ast-grep'; a candidate that fails for any
reason is rejected and resolution continues to the next tier.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from config.features import AST_GREP
from config.path import ROOT_DIR

_resolution_cache: str | None = None


def _binary_name(platform: str) -> str:
    return "sg.exe" if platform == "win32" else "sg"


def _ast_grep_binary_name(platform: str) -> str:
    return "ast-grep.exe" if platform == "win32" else "ast-grep"


def _runtime_slug() -> str:
    platform = sys.platform
    arch = os.environ.get("SHERRY_ARCH") or os.arch if hasattr(os, "arch") else ""
    # Fallback to platform.machine()
    if not arch:
        import platform as pf
        arch = pf.machine()
    arch_norm = "arm64" if arch in ("arm64", "aarch64") else "x64"
    plat_norm = "win32" if platform == "win32" else ("darwin" if platform == "darwin" else "linux")
    return f"{plat_norm}-{arch_norm}"


def _executable_suffixes() -> list[str]:
    if sys.platform != "win32":
        return [""]
    pathext = os.environ.get("PATHEXT", "")
    suffixes = [e.lower() for e in pathext.split(";") if e]
    return list(dict.fromkeys(["", *suffixes, ".exe", ".cmd", ".bat"]))


def _probe_version(binary_path: str) -> bool:
    """Run --version, return True if output contains 'ast-grep'."""
    try:
        result = subprocess.run(
            [binary_path, "--version"],
            capture_output=True, text=True,
            timeout=AST_GREP["ast_grep_version_probe_timeout_ms"] / 1000,
        )
        return "ast-grep" in (result.stdout + result.stderr).lower()
    except Exception:
        return False


def _candidate_exists(path: str) -> bool:
    p = Path(path)
    if not p.exists():
        return False
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _accepts(binary_path: str) -> bool:
    return _candidate_exists(binary_path) and _probe_version(binary_path)


def _env_override_candidates() -> list[str]:
    key = AST_GREP["ast_grep_path_env_key"]
    val = os.environ.get(key, "").strip()
    return [val] if val else []


def _runtime_candidates() -> list[str]:
    import os
    home = Path.home()
    slug = _runtime_slug()
    name = _binary_name(sys.platform)
    runtime_dir = AST_GREP["ast_grep_runtime_dir"] or str(home / ".sherry" / "runtime" / "ast-grep" / slug)
    return [str(Path(runtime_dir) / name)]


def _skill_bin_candidates() -> list[str]:
    from config.path import ROOT_DIR
    names = [_ast_grep_binary_name(sys.platform), _binary_name(sys.platform)]
    bin_dir = ROOT_DIR / "skills" / "ast-grep" / "bin"
    return [str(bin_dir / n) for n in names]


def _path_candidates() -> list[str]:
    """Find ast-grep / sg on PATH."""
    names = [_ast_grep_binary_name(sys.platform), _binary_name(sys.platform)]
    found = []
    for name in names:
        path = _which(name)
        if path:
            found.append(path)
    return found


def _which(command: str) -> str | None:
    """Cross-platform which(1)."""
    suffixes = _executable_suffixes()
    path_env = os.environ.get("PATH") or os.environ.get("Path") or ""
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        for suffix in suffixes:
            candidate = Path(entry) / (command + suffix)
            if candidate.exists():
                return str(candidate)
    return None


def _homebrew_candidates() -> list[str]:
    if sys.platform == "darwin":
        prefixes = ["/opt/homebrew/bin", "/usr/local/bin"]
    elif sys.platform == "linux":
        prefixes = ["/home/linuxbrew/.linuxbrew/bin", "/usr/local/bin"]
    else:
        return []
    names = [_ast_grep_binary_name(sys.platform), _binary_name(sys.platform)]
    return [str(Path(p) / n) for p in prefixes for n in names]


def _first_accepted(candidates: list[str]) -> str | None:
    for c in candidates:
        if _accepts(c):
            return c
    return None


def resolve_sg_binary() -> str | None:
    """Resolve ast-grep binary across 5 tiers, or None if not found.

    Results cached per-process.
    """
    global _resolution_cache
    if _resolution_cache is not None and _candidate_exists(_resolution_cache):
        return _resolution_cache

    all_candidates: list[str] = []
    all_candidates.extend(_env_override_candidates())
    all_candidates.extend(_runtime_candidates())
    all_candidates.extend(_skill_bin_candidates())
    # PATH tier
    all_candidates.extend(_path_candidates())
    # Homebrew tier
    all_candidates.extend(_homebrew_candidates())

    result = _first_accepted(all_candidates)
    if result:
        _resolution_cache = result
    return result


def _clear_cache_for_tests() -> None:
    global _resolution_cache
    _resolution_cache = None
```

#### 4. `agent/tools/code_intel/ast_grep/provisioner.py` — SHA-256 验证下载

```python
"""ast-grep binary auto-provisioning — SHA-256 verified GitHub download.

Mirrors oh-my-openagent sg-provisioner.ts:
  1. Download pinned release ZIP from GitHub
  2. SHA-256 verify the archive
  3. Extract standalone sg/ast-grep binary from ZIP
  4. Write to ~/.sherry/runtime/ast-grep/<slug>/sg (chmod 755)
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

from loguru import logger

from config.features import AST_GREP
from .resolver import _runtime_slug, _binary_name, resolve_sg_binary, _clear_cache_for_tests


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).digest().hex()


def _download(url: str, timeout_s: float) -> bytes:
    """Download URL content, return bytes."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "sherry-agent"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.read()


def _extract_binary(zip_bytes: bytes, platform: str) -> bytes:
    """Extract sg/ast-grep binary from ZIP archive."""
    suffix = ".exe" if platform == "win32" else ""
    preferred_names = [f"ast-grep{suffix}", f"sg{suffix}"]

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            basename = name.split("/")[-1]
            if basename in preferred_names:
                return zf.read(name)
    raise FileNotFoundError(f"No standalone {' or '.join(preferred_names)} binary in ZIP")


def provision_sg_binary() -> str | None:
    """Download, verify, and install the pinned ast-grep binary.

    Returns binary path on success, None on failure.
    """
    slug = _runtime_slug()
    asset = AST_GREP["ast_grep_release_assets"].get(slug)
    if not asset:
        logger.error("ast-grep {} has no release asset for {}", AST_GREP["ast_grep_pinned_version"], slug)
        return None

    home = Path.home()
    runtime_dir = Path(AST_GREP["ast_grep_runtime_dir"] or str(home / ".sherry" / "runtime" / "ast-grep" / slug))
    binary_name = _binary_name(sys.platform)
    destination = runtime_dir / binary_name

    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("Cannot create ast-grep runtime dir {}: {}", runtime_dir, e)
        return None

    try:
        archive = _download(asset["url"], AST_GREP["ast_grep_provision_timeout_s"])
    except Exception as e:
        logger.error("Failed to download ast-grep {}: {}", AST_GREP["ast_grep_pinned_version"], e)
        return None

    actual_sha = _sha256(archive)
    if actual_sha != asset["sha256"]:
        logger.error(
            "ast-grep checksum mismatch: expected {}, got {}",
            asset["sha256"][:16], actual_sha[:16],
        )
        return None

    try:
        binary_bytes = _extract_binary(archive, sys.platform)
    except (FileNotFoundError, zipfile.BadZipFile) as e:
        logger.error("Failed to extract ast-grep binary: {}", e)
        return None

    try:
        destination.write_bytes(binary_bytes)
        if sys.platform != "win32":
            os.chmod(destination, 0o755)
    except OSError as e:
        logger.error("Failed to write ast-grep binary to {}: {}", destination, e)
        return None

    logger.info("ast-grep {} provisioned to {}", AST_GREP["ast_grep_pinned_version"], destination)
    _clear_cache_for_tests()
    return str(destination)
```

#### 5. `agent/tools/code_intel/ast_grep/install_hints.py` — 安装提示

```python
"""ast-grep install hints — returned to caller when binary not found.

Mirrors oh-my-openagent sg-install-hints.ts.
"""

import sys

_SHERRY_PROVISION_HINT = "Start a sherry session so the bundled ast-grep provisions the pinned runtime automatically"
_ENV_OVERRIDE_HINT = "Or point SHERRY_SG_PATH at an existing ast-grep binary"

_DARWIN_HINTS = [
    "brew install ast-grep",
    "npm install -g @ast-grep/cli",
    "cargo install ast-grep --locked",
]

_LINUX_HINTS = [
    "npm install -g @ast-grep/cli",
    "cargo install ast-grep --locked",
    "brew install ast-grep  # linuxbrew",
]

_WIN32_HINTS = [
    "scoop install main/ast-grep",
    "winget install ast-grep",
    "choco install ast-grep",
    "npm install -g @ast-grep/cli",
]


def sg_install_hints(platform: str = sys.platform) -> list[str]:
    if platform == "darwin":
        base = _DARWIN_HINTS
    elif platform == "win32":
        base = _WIN32_HINTS
    else:
        base = _LINUX_HINTS
    return [*base, _SHERRY_PROVISION_HINT, _ENV_OVERRIDE_HINT]


def sg_binary_not_found_message(platform: str = sys.platform) -> str:
    return (
        f"ast-grep binary not found for {platform}: no candidate passed the "
        f"--version probe across the env override, sherry runtime, skill bin "
        f"cache, PATH, or Homebrew prefixes."
    )
```

#### 6. `agent/tools/code_intel/ast_grep/runner.py` — 工具实现

```python
"""ast-grep runner: structural search + rewrite via sg CLI subprocess.

Tools are injected into ALL subagents (core component, not RESEARCHER-only).
Mirrors oh-my-openagent ast-grep-mcp/src/tools/search.ts + rewrite.ts.
"""

import json
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from config.features import AST_GREP
from .resolver import resolve_sg_binary
from .provisioner import provision_sg_binary
from .install_hints import sg_install_hints, sg_binary_not_found_message


class SgSearchInput(BaseModel):
    pattern: str = Field(description="AST pattern (code, not regex). Must parse as one AST node.")
    language: str = Field(description="Language of the pattern.")
    paths: list[str] = Field(description="Root paths to search (1-64).")
    globs: list[str] | None = Field(None, description="File glob filters.")
    strictness: str | None = Field(None, description="cst | smart | ast | relaxed | signature")
    max_matches: int | None = Field(None, description="Max results (default 50)")


class SgRewriteInput(BaseModel):
    pattern: str = Field(description="AST pattern to match.")
    rewrite: str = Field(description="Replacement pattern (can use $VAR from pattern).")
    language: str = Field(description="Language.")
    paths: list[str] = Field(description="Root paths.")
    dry_run: bool = Field(True, description="Preview only (default true). Set false to apply.")


def _ensure_binary() -> str | None:
    """Resolve or provision the ast-grep binary."""
    binary = resolve_sg_binary()
    if binary:
        return binary
    # Try auto-provision
    return provision_sg_binary()


def _build_search_args(
    pattern: str, language: str, paths: list[str],
    globs: list[str] | None, strictness: str | None,
) -> list[str]:
    args = ["run", "-p", pattern, "--lang", language, "--json=stream"]
    args += ["--strictness", strictness or AST_GREP["ast_grep_strictness_default"]]
    if globs:
        for g in globs:
            args += ["--globs", g]
    args += paths
    return args


def _run_sg(binary: str, args: list[str], workdir: str, timeout_ms: int) -> dict:
    try:
        proc = subprocess.run(
            [binary, *args],
            capture_output=True, text=True, cwd=workdir,
            timeout=timeout_ms / 1000,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ast-grep timed out after {timeout_ms}ms"}
    except FileNotFoundError:
        return {"ok": False, "error": "ast-grep binary not found"}

    if proc.returncode != 0 and not proc.stdout:
        return {"ok": False, "error": proc.stderr.strip()}

    records = []
    for line in proc.stdout.strip().splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"ok": True, "matches": records}


@tool("ast_grep_search", args_schema=SgSearchInput)
def _ast_grep_search_tool(
    pattern: str,
    language: str,
    paths: list[str],
    globs: list[str] | None = None,
    strictness: str | None = None,
    max_matches: int | None = None,
    session_id: str = "",
) -> str:
    """Structural code search with ast-grep. Pattern is code, not regex.
    $NAME matches one node, $$$NAME matches zero-or-more nodes.
    Example: ast_grep_search(pattern="def $FUNC($$$): $$$BODY", language="python", paths=["src/"])"""
    binary = _ensure_binary()
    if not binary:
        hints = sg_install_hints()
        return json.dumps({
            "error": sg_binary_not_found_message(),
            "install_hints": hints,
        }, ensure_ascii=False)

    result = _run_sg(
        binary,
        _build_search_args(pattern, language, paths, globs, strictness),
        workdir=paths[0],
        timeout_ms=AST_GREP["ast_grep_timeout_ms"],
    )
    if not result["ok"]:
        return f"Search failed: {result['error']}"
    matches = result["matches"][: max_matches or AST_GREP["ast_grep_max_matches"]]
    return json.dumps({"matches": matches, "count": len(matches)}, ensure_ascii=False)


@tool("ast_grep_rewrite", args_schema=SgRewriteInput)
def _ast_grep_rewrite_tool(
    pattern: str,
    rewrite: str,
    language: str,
    paths: list[str],
    dry_run: bool = True,
    session_id: str = "",
) -> str:
    """Structural code rewrite with ast-grep. Use $VAR from pattern in rewrite.
    dry_run=true (default) previews; set false to apply.
    Example: ast_grep_rewrite(pattern="print($MSG)", rewrite="logger.info($MSG)", language="python", paths=["src/"])"""
    binary = _ensure_binary()
    if not binary:
        hints = sg_install_hints()
        return json.dumps({
            "error": sg_binary_not_found_message(),
            "install_hints": hints,
        }, ensure_ascii=False)

    args = ["run", "-p", pattern, "-r", rewrite, "--lang", language, "--json=stream"]
    if dry_run:
        args.append("--dry-run")
    args += paths

    result = _run_sg(binary, args, workdir=paths[0], timeout_ms=AST_GREP["ast_grep_timeout_ms"])
    if not result["ok"]:
        return f"Rewrite failed: {result['error']}"
    return json.dumps({
        "changes": result["matches"],
        "applied": not dry_run,
        "count": len(result["matches"]),
    }, ensure_ascii=False)


def build_ast_grep_tools(session_id: str) -> list[BaseTool]:
    """Build ast-grep tools — injected into ALL subagents (core component)."""
    return [_ast_grep_search_tool, _ast_grep_rewrite_tool]
```

#### 7. `skills/ast-grep/install.sh` + `install.ps1` — 安装脚本

> 参照 oh-my-openagent `shared-skills/skills/ast-grep/install.sh`（286 行），
> 7 路包管理器 fallback + GitHub tarball 下载。
> 此处不展开完整脚本，结构与 omo 的 install.sh 完全一致：
> brew → npm → cargo → pip → nix → mise → GitHub release tarball。

### 注入点

**所有 subagent 均注入**（对标 oh-my-openagent 全局 MCP server）：

在原计划 `spawn/core.py::_build_child_agent()` 中，ast-grep 工具的注入
**不限于 RESEARCHER 角色**，而是所有 functional_role 均可使用：

```python
# spawn/core.py — 所有 subagent 均注入 ast-grep（核心组件）
from agent.tools.code_intel.ast_grep import build_ast_grep_tools

ast_grep_tools = build_ast_grep_tools(session_id=run.child_session_key)

if functional_role == FunctionalRole.RESEARCHER:
    from agent.tools.code_intel import build_code_intel_tools
    code_intel_tools = build_code_intel_tools(session_id=run.child_session_key)
    final_tools = [*filtered_tools, *code_intel_tools, *ast_grep_tools]
else:
    # 非 RESEARCHER 角色：注入 ast-grep（结构化搜索/重写是通用能力）
    final_tools = [*filtered_tools, *ast_grep_tools]
```

> **设计决策：** oh-my-openagent 中 ast-grep 是全局 MCP server，所有 agent
> 均可访问。sherry 中同样将其注入所有 subagent，不限制为 RESEARCHER。
> tree-sitter explore/callers/callees/impact 仍为 RESEARCHER-only（需要索引上下文），
> 但 ast-grep 的结构化搜索/重写是通用的代码操作能力。

### 工具

| 工具               | 说明                                          | 参考                                                |
| ------------------ | --------------------------------------------- | --------------------------------------------------- |
| `ast_grep_search`  | 结构化模式搜索（25 语言, 元变量, strictness） | oh-my-openagent `ast-grep-mcp/src/tools/search.ts`  |
| `ast_grep_rewrite` | 结构化模式重写（dry_run 默认预览）            | oh-my-openagent `ast-grep-mcp/src/tools/rewrite.ts` |

### 工期

| 步骤     | 内容                                                     | 预估     |
| -------- | -------------------------------------------------------- | -------- |
| 1        | `config/features/agent_side/ast_grep.py` + re-exports    | 0.5h     |
| 2        | `agent/tools/code_intel/ast_grep/resolver.py`            | 3h       |
| 3        | `agent/tools/code_intel/ast_grep/provisioner.py`         | 2h       |
| 4        | `agent/tools/code_intel/ast_grep/install_hints.py`       | 0.5h     |
| 5        | `agent/tools/code_intel/ast_grep/runner.py`              | 2h       |
| 6        | `skills/ast-grep/install.sh` + `install.ps1`             | 1h       |
| 7        | 修改 `spawn/core.py` 注入 ast_grep 工具（所有 subagent） | 0.5h     |
| 8        | 测试（5 文件）                                           | 3h       |
| 9        | ruff + basedpyright + pytest                             | 0.5h     |
| **小计** |                                                          | **~13h** |

---

## Phase 2S — LSP 二进制发现与自动安装（替代原 Phase 2）

> **前置条件：Phase 1 + Phase 1A 完成。**
> **替代原计划 Phase 2。** 原计划的 4 个 LSP 工具保留不变，
> 本阶段补充原计划完全缺失的 LSP 服务器发现、安装、fallback 基础设施。

### 架构

```
┌─ LSP 服务器发现层 ──────────────────────────────────────────────┐
│                                                                  │
│  resolve_lsp_server(language, cwd)                               │
│  ├─ 1. 显式路径（LSP_*_SERVER 配置为绝对路径）                    │
│  ├─ 2. repo-local 二进制（.venv/bin, node_modules/.bin, ...）     │
│  │     └─ marker-gated: package.json / pyproject.toml / go.mod   │
│  ├─ 3. PATH 查找（含 Windows PATHEXT 后缀匹配）                   │
│  └─ 4. not_installed → 返回安装提示 + fallback 信号               │
│                                                                  │
│  install_lsp_server(language)                                     │
│  ├─ 查 AUTO_INSTALLABLE_SERVERS 获取安装命令                      │
│  ├─ 执行安装（subprocess, 超时 60s, 日志捕获）                     │
│  └─ 安装后重新 resolve_lsp_server                                  │
│                                                                  │
│  LSP fallback 链                                                  │
│  ├─ LSP 可用 → lsp_goto_definition / lsp_find_references          │
│  ├─ LSP 不可用 → tree-sitter explore (Phase 1)                    │
│  └─ tree-sitter 无匹配 → search_files (regex grep)                │
└──────────────────────────────────────────────────────────────────┘
```

### 新增文件（4 个）

#### 1. `config/features/agent_side/lsp.py`（扩展原计划的 LspConfig）

```python
"""LSP server configuration — binary paths, timeouts, install hints."""

from typing import TypedDict


class LspConfig(TypedDict):
    """Configuration for LSP tools and server provisioning."""
    # 原计划字段（保留）
    lsp_python_server: str
    lsp_typescript_server: str
    lsp_rust_server: str
    lsp_go_server: str
    lsp_request_timeout_s: float
    lsp_server_start_timeout_s: float
    lsp_enabled_languages: list[str]
    # ── 补充字段 ──
    lsp_install_timeout_s: float           # 自动安装超时（默认 60）
    lsp_auto_install: bool                 # 是否允许自动安装（默认 False，需用户授权）
    lsp_resolve_cache_enabled: bool        # 二进制发现缓存（默认 True）
    lsp_repo_local_bin_rules: dict         # marker → bin_dirs 映射
    lsp_supported_servers: dict            # 语言 → server 配置
    lsp_install_hints: dict                # 语言 → 安装提示文本
    lsp_auto_install_commands: dict        # 语言 → 安装命令列表


LSP: LspConfig = {
    # 原计划字段（保留）
    "lsp_python_server": "basedpyright-langserver",
    "lsp_typescript_server": "typescript-language-server",
    "lsp_rust_server": "rust-analyzer",
    "lsp_go_server": "gopls",
    "lsp_request_timeout_s": 10.0,
    "lsp_server_start_timeout_s": 15.0,
    "lsp_enabled_languages": ["python", "typescript", "rust", "go"],
    # ── 补充字段 ──
    "lsp_install_timeout_s": 60.0,
    "lsp_auto_install": False,
    "lsp_resolve_cache_enabled": True,
    # repo-local bin 目录发现规则（参考 oh-my-openagent server-installation.ts）
    # marker 文件存在时才信任对应的 bin 目录
    "lsp_repo_local_bin_rules": {
        "python": {
            "markers": [
                "pyproject.toml", "requirements.txt", "setup.py",
                "setup.cfg", "pyrightconfig.json",
            ],
            "bin_dirs": [".venv/bin", ".venv/Scripts", "venv/bin", "venv/Scripts"],
        },
        "typescript": {
            "markers": [
                "package.json", "bun.lock", "package-lock.json",
                "yarn.lock", "pnpm-lock.yaml",
            ],
            "bin_dirs": ["node_modules/.bin"],
        },
        "rust": {
            "markers": ["Cargo.toml"],
            "bin_dirs": ["target/debug", "target/release"],
        },
        "go": {
            "markers": ["go.mod", "go.sum", "go.work"],
            "bin_dirs": ["bin"],
        },
    },
    # 语言 → LSP server 命令 + 文件扩展名
    "lsp_supported_servers": {
        "python": {
            "command": ["basedpyright-langserver", "--stdio"],
            "extensions": [".py", ".pyi"],
            "local_install": "uv add --dev basedpyright",
        },
        "typescript": {
            "command": ["typescript-language-server", "--stdio"],
            "extensions": [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"],
            "local_install": "bun add -d typescript-language-server typescript",
        },
        "rust": {
            "command": ["rust-analyzer"],
            "extensions": [".rs"],
            "local_install": None,  # 工具链级，无项目级安装
        },
        "go": {
            "command": ["gopls"],
            "extensions": [".go"],
            "local_install": None,
        },
    },
    # 安装提示（not_installed 时返回给 subagent / 用户）
    "lsp_install_hints": {
        "python": "pip install basedpyright",
        "typescript": "npm install -g typescript-language-server typescript",
        "rust": "rustup component add rust-analyzer",
        "go": "go install golang.org/x/tools/gopls@latest",
    },
    # 自动安装命令（lsp_auto_install=True 且用户授权时执行）
    "lsp_auto_install_commands": {
        "python": ["pip", "install", "basedpyright"],
        "typescript": ["npm", "install", "-g", "typescript-language-server", "typescript"],
        "go": ["go", "install", "golang.org/x/tools/gopls@latest"],
        # rust-analyzer 经由 rustup，不走 pip/npm，不列入自动安装
    },
}
```

**遵循约定：** 一个 TypedDict + 一个实例，re-export via `__init__.py`。

#### 2. `agent/tools/code_intel/lsp/resolver.py` — 二进制发现

```python
"""LSP server binary resolution — multi-layer discovery with caching.

Probing order (mirrors oh-my-openagent server-installation.ts):
  1. Explicit path (config value is absolute) → validate exists
  2. Repo-local bin dirs (marker-gated: pyproject.toml → .venv/bin, package.json → node_modules/.bin)
  3. PATH lookup (with Windows PATHEXT suffixes)
  4. None → not_installed (caller returns install hint + fallback)

Results cached per-process, keyed by (cwd, command, platform).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from config.features import LSP

_resolution_cache: dict[str, str | None] = {}


def _executable_suffixes() -> list[str]:
    """Platform-appropriate executable suffixes."""
    if sys.platform != "win32":
        return [""]
    pathext = os.environ.get("PATHEXT", "")
    suffixes = [e.lower() for e in pathext.split(";") if e]
    return list(dict.fromkeys(["", *suffixes, ".exe", ".cmd", ".bat"]))


def _probe(directory: str, command: str, suffixes: list[str]) -> str | None:
    for suffix in suffixes:
        candidate = Path(directory) / (command + suffix)
        if candidate.exists():
            return str(candidate)
    return None


def _resolve_local(
    command: str, cwd: str, suffixes: list[str], language: str
) -> str | None:
    """Walk up from cwd, checking marker-gated bin dirs."""
    rules = LSP["lsp_repo_local_bin_rules"].get(language, {})
    markers = rules.get("markers", [])
    bin_dirs = rules.get("bin_dirs", [])

    current = Path(cwd).resolve()
    while True:
        # Check if any marker exists at this level
        if any((current / m).exists() for m in markers):
            for bin_dir in bin_dirs:
                found = _probe(str(current / bin_dir), command, suffixes)
                if found:
                    return found

        # Stop at repo root
        if (current / ".git").exists():
            return None

        parent = current.parent
        if parent == current:
            return None
        current = parent


def _resolve_from_path(command: str, suffixes: list[str]) -> str | None:
    path_env = os.environ.get("PATH") or os.environ.get("Path") or ""
    for entry in path_env.split(os.pathsep):
        if not entry:
            continue
        found = _probe(entry, command, suffixes)
        if found:
            return found
    return None


def resolve_lsp_server(language: str, cwd: str | None = None) -> str | None:
    """Resolve LSP server binary path, or None if not installed.

    Args:
        language: One of LSP["lsp_enabled_languages"].
        cwd: Working directory for repo-local resolution.

    Returns:
        Absolute binary path, or None.
    """
    server_config = LSP["lsp_supported_servers"].get(language)
    if not server_config:
        return None
    command = server_config["command"][0]
    work_dir = cwd or os.getcwd()

    if not LSP["lsp_resolve_cache_enabled"]:
        return _resolve_uncached(command, work_dir, language)

    cache_key = f"{work_dir}:{command}:{sys.platform}"
    if cache_key in _resolution_cache:
        return _resolution_cache[cache_key]

    result = _resolve_uncached(command, work_dir, language)
    _resolution_cache[cache_key] = result
    return result


def _resolve_uncached(command: str, work_dir: str, language: str) -> str | None:
    suffixes = _executable_suffixes()

    # 1. Explicit absolute path
    if os.path.isabs(command):
        return command if Path(command).exists() else None

    # 2. Repo-local bin dirs (marker-gated)
    local = _resolve_local(command, work_dir, suffixes, language)
    if local:
        return local

    # 3. PATH
    return _resolve_from_path(command, suffixes)


def get_install_hint(language: str) -> str:
    """Return human-readable install hint for a language's LSP server."""
    return LSP["lsp_install_hints"].get(
        language, f"Install the LSP server for {language} and ensure it's in PATH."
    )


def get_local_install_hint(language: str) -> str | None:
    """Return repo-local install command (devDependency), or None."""
    server = LSP["lsp_supported_servers"].get(language, {})
    return server.get("local_install")


def _reset_cache_for_tests() -> None:
    _resolution_cache.clear()
```

#### 3. `agent/tools/code_intel/lsp/installer.py` — 自动安装

```python
"""LSP server auto-install — subprocess execution with timeout + log capture."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from loguru import logger

from config.features import LSP
from agent.tools.pub_base.env_scrub import scrub_env
from .resolver import resolve_lsp_server, get_install_hint


def install_lsp_server(language: str, cwd: str | None = None) -> dict:
    """Install an LSP server for the given language.

    Returns:
        {"ok": bool, "binary_path": str | None, "message": str}
    """
    if not LSP["lsp_auto_install"]:
        hint = get_install_hint(language)
        return {
            "ok": False,
            "binary_path": None,
            "message": f"Auto-install disabled. Install manually: {hint}",
        }

    command = LSP["lsp_auto_install_commands"].get(language)
    if not command:
        hint = get_install_hint(language)
        return {
            "ok": False,
            "binary_path": None,
            "message": f"No auto-install command for {language}. Install manually: {hint}",
        }

    timeout = LSP["lsp_install_timeout_s"]
    env = scrub_env(os.environ.copy())

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or os.getcwd(),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "binary_path": None,
            "message": f"Install timed out after {timeout}s: {' '.join(command)}",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "binary_path": None,
            "message": f"Install tool not found: {command[0]}. Is it on PATH?",
        }

    if proc.returncode != 0:
        return {
            "ok": False,
            "binary_path": None,
            "message": f"Install failed (exit {proc.returncode}): {proc.stderr[:500]}",
        }

    logger.info("LSP server installed for {}: {}", language, " ".join(command))

    # Re-resolve after install
    binary_path = resolve_lsp_server(language, cwd)
    # Clear cache so re-resolution sees the new binary
    from .resolver import _reset_cache_for_tests
    _reset_cache_for_tests()
    binary_path = resolve_lsp_server(language, cwd)

    return {
        "ok": binary_path is not None,
        "binary_path": binary_path,
        "message": f"Installed {language} LSP server" if binary_path else "Install ran but binary not found on PATH",
    }
```

#### 4. `agent/tools/code_intel/lsp/fallback.py` — fallback 链

```python
"""LSP fallback chain — graceful degradation when LSP is unavailable.

Resolution order:
  1. LSP server available → use LSP tools (goto_definition, find_references, ...)
  2. LSP not installed → return install hint + suggest tree-sitter explore (Phase 1)
  3. tree-sitter no match → suggest search_files (regex grep)
"""

from __future__ import annotations

from typing import Literal

from .resolver import resolve_lsp_server, get_install_hint, get_local_install_hint


type LspAvailability = Literal["available", "not_installed", "not_configured"]


def check_lsp_availability(language: str, cwd: str | None = None) -> tuple[LspAvailability, str]:
    """Check if LSP is available for a language.

    Returns:
        (status, message)
        - ("available", binary_path)
        - ("not_installed", install_hint)
        - ("not_configured", "Language {language} not in enabled list")
    """
    from config.features import LSP as LSP_CONFIG

    if language not in LSP_CONFIG["lsp_enabled_languages"]:
        return "not_configured", f"Language '{language}' not in LSP enabled list: {LSP_CONFIG['lsp_enabled_languages']}"

    binary = resolve_lsp_server(language, cwd)
    if binary:
        return "available", binary

    hint = get_install_hint(language)
    local_hint = get_local_install_hint(language)
    if local_hint:
        hint = f"{hint} (project-local: {local_hint})"
    return "not_installed", hint


def build_fallback_message(
    language: str,
    requested_tool: str,
    availability: LspAvailability,
    message: str,
) -> str:
    """Build a user-facing message when LSP is unavailable.

    Suggests the fallback path:
      LSP unavailable → try explore (tree-sitter) → try search_files
    """
    if availability == "not_configured":
        return (
            f"{requested_tool}: {message}. "
            f"Fallback: use `explore` (tree-sitter symbol index) or `search_files` (regex)."
        )
    if availability == "not_installed":
        return (
            f"{requested_tool}: LSP server for {language} is not installed. "
            f"Install: {message}. "
            f"Fallback: use `explore` (tree-sitter) or `search_files` (regex) for now."
        )
    return ""
```

### 原计划 Phase 2 文件（保留不变）

原计划的以下文件保留，但需修改 `client.py` 以使用 `resolver.py` 发现的二进制路径：

| 文件                                     | 修改                                                                                        |
| ---------------------------------------- | ------------------------------------------------------------------------------------------- |
| `agent/tools/code_intel/lsp/protocol.py` | 不变                                                                                        |
| `agent/tools/code_intel/lsp/client.py`   | 启动前调用 `resolve_lsp_server()` 发现二进制；not_installed 时调用 `fallback.py`            |
| `agent/tools/code_intel/lsp/tools.py`    | 每个工具调用前检查 `check_lsp_availability()`，not_installed 时返回安装提示 + fallback 建议 |

### `client.py` 修改要点

```python
# 原计划 client.py 直接用 config 中的 server 名启动子进程
# 修改后：先 resolve_lsp_server() 发现二进制，再启动

from .resolver import resolve_lsp_server, check_lsp_availability
from .fallback import build_fallback_message

class LSPClient:
    def __init__(self, language: str, cwd: str):
        self._language = language
        self._cwd = cwd
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> str | None:
        """Start LSP server. Returns error message (None = success)."""
        availability, msg = check_lsp_availability(self._language, self._cwd)
        if availability != "available":
            return build_fallback_message(
                self._language, "lsp_start", availability, msg
            )
        # resolve_lsp_server 返回绝对路径
        binary_path = msg  # "available" 状态下 msg = binary path
        server_config = LSP["lsp_supported_servers"][self._language]
        command = [binary_path, *server_config["command"][1:]]

        env = scrub_env(os.environ.copy())
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self._cwd,
        )
        ...
```

### `tools.py` 修改要点

```python
# 每个工具在执行 LSP 请求前检查可用性

@tool("lsp_goto_definition")
def _lsp_goto_definition_tool(
    file_path: str, line: int, character: int, session_id: str = ""
) -> str:
    """Jump to the definition of a symbol."""
    language = _detect_language(file_path)
    availability, msg = check_lsp_availability(language)
    if availability != "available":
        return build_fallback_message(
            language, "lsp_goto_definition", availability, msg
        )
    # ... proceed with LSP request ...
```

---

## Phase 2X — LSP 语言与工具扩展

> **前置条件：Phase 2S 完成。**
> **目标：** 将 LSP 覆盖从 4 语言 + 4 工具扩展到 12+ 语言 + 8 工具。

### 新增语言

在 `LSP["lsp_supported_servers"]` 中追加：

| 语言  | LSP Server             | 安装命令                                              | 文件扩展名                 |
| ----- | ---------------------- | ----------------------------------------------------- | -------------------------- |
| C/C++ | `clangd`               | `See https://clangd.llvm.org/installation`            | `.c .cpp .cc .cxx .h .hpp` |
| Java  | `jdtls`                | `See https://github.com/eclipse-jdtls/eclipse.jdt.ls` | `.java`                    |
| Ruby  | `ruby-lsp`             | `gem install ruby-lsp`                                | `.rb .rake`                |
| Bash  | `bash-language-server` | `npm install -g bash-language-server`                 | `.sh .bash .zsh`           |
| Vue   | `vue-language-server`  | `npm install -g @vue/language-server`                 | `.vue`                     |
| YAML  | `yaml-language-server` | `npm install -g yaml-language-server`                 | `.yaml .yml`               |

> **设计约束：** 不预装所有 LSP server。`lsp_enabled_languages` 由用户配置控制，
> 默认仅启用已安装的语言。`resolve_lsp_server()` 发现二进制存在时自动启用。

### 新增工具

| 工具              | LSP 方法                          | 说明                                    | 优先级 |
| ----------------- | --------------------------------- | --------------------------------------- | ------ |
| `lsp_rename`      | `textDocument/rename`             | 跨工作区符号重命名                      | P1     |
| `lsp_diagnostics` | `textDocument/publishDiagnostics` | 实时诊断（错误/警告）                   | P1     |
| `lsp_format`      | `textDocument/rangeFormatting`    | 代码格式化                              | P2     |
| `lsp_status`      | — (内部聚合)                      | 列出所有已配置/已安装/活跃的 LSP 服务器 | P2     |

> `lsp_rename` 和 `lsp_diagnostics` 是代码编辑中最常用的 LSP 功能，
> 缺少它们意味着 subagent 无法完成"重构前检查"和"重命名"工作流。

### 工期

| 步骤     | 内容                                                                             | 预估    |
| -------- | -------------------------------------------------------------------------------- | ------- |
| 1        | 扩展 `lsp_supported_servers` / `lsp_install_hints` / `lsp_auto_install_commands` | 0.5h    |
| 2        | `lsp/protocol.py` 追加 rename / diagnostics 数据结构                             | 1h      |
| 3        | `lsp/tools.py` 追加 4 个工具                                                     | 3h      |
| 4        | 修改 `spawn/core.py` + `system_prompt.py`（注入新工具）                          | 0.3h    |
| 5        | 测试                                                                             | 2h      |
| 6        | ruff + basedpyright + pytest                                                     | 0.5h    |
| **小计** |                                                                                  | **~7h** |

---

## Phase 4 — 外部代码检索 subagent

> **前置条件：subagent 功能角色分工（已落地）+ 本计划 Phase 1A 完成。**
> **定位：** 对标 oh-my-openagent 的 `librarian` subagent。
> 专门检索**外部代码库**（GitHub 仓库、npm 包、官方文档），
> 与 Phase 1-2 的**内部**代码检索互补。

### 架构

```
┌─ Librarian Subagent（RESEARCHER 角色，只读）──────────────────────┐
│                                                                    │
│  输入：用户问"某库怎么用？" / "某库怎么实现的？"                     │
│                                                                    │
│  工具链（复用现有 + 新增）：                                         │
│  ├─ web_search          已有 — 搜索官方文档 URL                      │
│  ├─ web_fetch           已有 — 抓取文档页面                           │
│  ├─ terminal (git/gh)   已有 — clone repo, git blame, gh search      │
│  ├─ search_files        已有 — 搜索 clone 下来的仓库                  │
│  ├─ read_file           已有 — 读取 clone 的源码                     │
│  └─ explore (Phase 1)   已有 — tree-sitter 索引 clone 的仓库         │
│                                                                    │
│  输出：                                                              │
│  ├─ 概念问题 → 官方文档摘要 + 链接                                   │
│  ├─ 实现问题 → 源码引用 + GitHub permalink                          │
│  └─ 上下文问题 → 相关 issues/PRs                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 设计决策

- **不新增工具** — librarian 复用现有的 `web_search`、`terminal`（git/gh）、`search_files`、`read_file`、`explore`
- **新增的是 subagent 角色定义** — 在 `FunctionalRole` 中定义 `RESEARCHER` 角色时，librarian 是一个预设的 RESEARCHER 实例
- **新增 `librarian` system prompt** — 类似 oh-my-openagent 的 librarian prompt，包含文档发现 → 仓库 clone → 源码搜索的工作流

### 新增文件（1 个）

#### `agent/tools/subagent/spawn/role_definitions/librarian.py`

```python
"""Librarian subagent role definition — external codebase retrieval.

Registered as a RESEARCHER-role subagent preset. The spawn system resolves
this definition when `agent_id="librarian"` is passed to sessions_spawn.
"""

from __future__ import annotations

LIBRARIAN_SYSTEM_PROMPT = """\
# THE LIBRARIAN

You are a specialized open-source codebase understanding agent.

## Your Mission

Answer questions about external libraries by finding EVIDENCE with
GitHub permalinks or official documentation links.

## Workflow

### TYPE A: Conceptual ("How do I use X?")
  1. web_search("library official documentation")
  2. web_fetch(specific doc page)
  3. Summarize with version-aware links

### TYPE B: Implementation ("How does X implement Y?")
  1. terminal: git clone --depth 1 to temp dir
  2. explore or search_files in cloned repo
  3. read_file for specific implementation
  4. Construct GitHub permalink: https://github.com/owner/repo/blob/<sha>/path#L10-L20

### TYPE C: Context ("Why was X changed?")
  1. terminal: gh search issues/prs
  2. terminal: git log --oneline -- path
  3. terminal: git blame -L start,end path

## Rules
- ALWAYS cite with permalinks (include commit SHA)
- Use --depth 1 for clones unless history is needed
- Clean up temp clones when done
- Read-only: never modify files in the target repo
"""

LIBRARIAN_TOOL_ALLOW = [
    "web_search", "web_fetch", "terminal", "read_file",
    "search_files", "explore", "callers", "callees", "impact",
    "ast_grep_search",
]

LIBRARIAN_TOOL_DENY = [
    "write", "edit", "patch_file", "task", "call_omo_agent",
    "sessions_spawn", "sessions_yield", "sessions_send",
]
```

### 注入点

在角色定义加载器中注册：

```python
# agent/tools/subagent/spawn/role_definitions/__init__.py
from .librarian import LIBRARIAN_SYSTEM_PROMPT, LIBRARIAN_TOOL_ALLOW, LIBRARIAN_TOOL_DENY

ROLE_DEFINITIONS: dict[str, RoleDefinition] = {
    # ... existing roles ...
    "librarian": RoleDefinition(
        system_prompt=LIBRARIAN_SYSTEM_PROMPT,
        tool_allow=LIBRARIAN_TOOL_ALLOW,
        tool_deny=LIBRARIAN_TOOL_DENY,
        functional_role=FunctionalRole.RESEARCHER,
    ),
}
```

### 工期

| 步骤     | 内容                                           | 预估      |
| -------- | ---------------------------------------------- | --------- |
| 1        | `role_definitions/librarian.py`                | 1h        |
| 2        | 注册到角色定义加载器                           | 0.3h      |
| 3        | 测试（spawn librarian subagent, 验证工具权限） | 1h        |
| **小计** |                                                | **~2.5h** |

---

## Phase 5 — 文件事件自动同步（可选）

> **前置条件：Phase 1 完成。**
> **定位：** 对标 oh-my-openagent CodeGraph 的 2s debounce 文件监听。
> 当前 sherry 的 tree-sitter 索引仅在查询时按 mtime 增量更新，
> 无实时性。本阶段添加文件监听 + 后台增量重索引。

### 设计

```
┌─ 文件监听器（进程级，server 启动时启动）──────────────────────────┐
│                                                                    │
│  watchfiles / asyncio 监听 ROOT_DIR（排除 prune_dirs）              │
│  ├─ 文件变更事件 → debounce 2s                                     │
│  ├─ debounce 后 → 增量重索引变更文件（mtime 检查）                  │
│  └─ 写入 .codeintel/db.sqlite                                      │
│                                                                    │
│  与查询路径的关系：                                                  │
│  └─ explore/callers/callees/impact 查询时仍做 mtime 检查            │
│     （监听器可能落后，mtime 是最终一致性保障）                       │
└────────────────────────────────────────────────────────────────────┘
```

### 依赖

```toml
# pyproject.toml [project] dependencies — 新增
"watchfiles>=2.0",  # Rust-based file watcher (asyncio)
```

### 新增文件（1 个）

#### `agent/tools/code_intel/watcher.py`

```python
"""Background file watcher for incremental re-indexing.

Started by the server process (not by subagents). Watches ROOT_DIR,
debounces file events, and triggers incremental re-indexing of changed
files in the .codeintel/db.sqlite symbol index.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger
from watchfiles import awatch

from config.features import CODE_INTEL
from config.path import ROOT_DIR
from .indexer import CodeIndexer


async def start_index_watcher(stop_event: asyncio.Event) -> None:
    """Start background file watcher for code intel index.

    Called once at server startup. Runs until stop_event is set.
    """
    if not CODE_INTEL.get("code_intel_index_db_path"):
        return  # code intel not configured

    indexer = CodeIndexer(
        CODE_INTEL["code_intel_index_db_path"],
        CODE_INTEL,
    )
    prune_dirs = set(CODE_INTEL["code_intel_prune_dirs"])
    debounce_s = 2.0

    async for changes in awatch(
        str(ROOT_DIR),
        stop_event=stop_event,
        watch_filter=lambda change, path: not any(
            p in path for p in prune_dirs
        ),
    ):
        # Debounce: collect changes, wait 2s, then batch re-index
        await asyncio.sleep(debounce_s)
        changed_files = [
            Path(path) for _, path in changes
            if Path(path).suffix in CODE_INTEL["code_intel_supported_extensions"]
        ]
        if changed_files:
            try:
                indexer.index_files(changed_files, ROOT_DIR)
                logger.debug("Re-indexed {} files", len(changed_files))
            except Exception:
                logger.exception("Background re-index failed")
```

### 注入点

在 `server/__main__.py` 或 `server/service/` 中启动：

```python
# server startup
import asyncio
from agent.tools.code_intel.watcher import start_index_watcher

stop_event = asyncio.Event()
# Start watcher as background task
asyncio.create_task(start_index_watcher(stop_event))
# On shutdown: stop_event.set()
```

### 工期

| 步骤     | 内容                                  | 预估    |
| -------- | ------------------------------------- | ------- |
| 1        | `pyproject.toml` 加 `watchfiles` 依赖 | 0.1h    |
| 2        | `agent/tools/code_intel/watcher.py`   | 2h      |
| 3        | 修改 server 启动逻辑                  | 0.5h    |
| 4        | 测试                                  | 1.5h    |
| **小计** |                                       | **~4h** |

---

## 修订后的总工期

| 阶段     | 内容                                             | 预估       | 前置            |
| -------- | ------------------------------------------------ | ---------- | --------------- |
| 前置     | subagent 功能角色分工（已落地）                  | —          | —               |
| Phase 1  | tree-sitter 符号索引 + 调用图 (原计划)           | ~15h       | 前置            |
| Phase 1A | ast-grep 结构化搜索 + 二进制 provision (必选)    | ~13h       | 前置            |
| Phase 2S | LSP 二进制发现 + 自动安装 + fallback (替代原 P2) | ~16h       | Phase 1 + 1A    |
| Phase 2X | LSP 语言 + 工具扩展 (新增)                       | ~7h        | Phase 2S        |
| Phase 3  | Embedding 语义搜索 (原计划)                      | ~8h        | Phase 1         |
| Phase 4  | 外部代码检索 subagent (新增)                     | ~2.5h      | Phase 1A + 前置 |
| Phase 5  | 文件事件自动同步 (可选, 新增)                    | ~4h        | Phase 1         |
| **合计** |                                                  | **~65.5h** |                 |

> 原计划 ~34h → 修订后 ~65.5h。增量 ~31.5h 主要来自 ast-grep 二进制 provision
> 基础设施（Phase 1A, +13h）和 LSP 基础设施（Phase 2S, +5h vs 原 Phase 2 的 ~11h → ~16h）。

### 推荐实施顺序

```
前置 (subagent 功能角色分工)
  ├→ Phase 1 (tree-sitter, 15h)
  └→ Phase 1A (ast-grep + provision, 13h) ← 与 Phase 1 并行
       ├→ Phase 2S (LSP 基础设施, 16h) ← 需 Phase 1 + 1A
       │    └→ Phase 2X (LSP 扩展, 7h)
       ├→ Phase 3 (Embedding, 8h) ← 可与 Phase 2S 并行
       └→ Phase 4 (librarian, 2.5h) ← 可与 Phase 2S 并行
            Phase 5 (file watcher, 4h) ← 可选，最后
```

---

## 修改文件清单（新增）

### Phase 1A 修改（7 个）

| 文件                                               | 修改                                    |
| -------------------------------------------------- | --------------------------------------- |
| `config/features/agent_side/ast_grep.py`           | 新建（含 provision 配置）               |
| `config/features/agent_side/__init__.py`           | re-export `AST_GREP` / `AstGrepConfig`  |
| `config/features/__init__.py`                      | 顶层 re-export                          |
| `agent/tools/code_intel/ast_grep/__init__.py`      | 新建                                    |
| `agent/tools/code_intel/ast_grep/resolver.py`      | 新建 — 5 层二进制发现                   |
| `agent/tools/code_intel/ast_grep/provisioner.py`   | 新建 — SHA-256 验证下载                 |
| `agent/tools/code_intel/ast_grep/install_hints.py` | 新建 — 安装提示                         |
| `agent/tools/code_intel/ast_grep/runner.py`        | 新建 — 工具实现                         |
| `skills/ast-grep/install.sh`                       | 新建 — POSIX 安装脚本（7 路 fallback）  |
| `skills/ast-grep/install.ps1`                      | 新建 — Windows 安装脚本                 |
| `agent/tools/subagent/spawn/core.py`               | 注入 ast_grep 工具（**所有 subagent**） |
| `agent/tools/subagent/spawn/system_prompt.py`      | 追加 ast-grep 使用指导                  |

### Phase 2S 修改（5 个）

| 文件                                      | 修改                               |
| ----------------------------------------- | ---------------------------------- |
| `config/features/agent_side/lsp.py`       | 扩展 LspConfig（+7 字段 + 3 dict） |
| `agent/tools/code_intel/lsp/resolver.py`  | 新建 — 二进制发现                  |
| `agent/tools/code_intel/lsp/installer.py` | 新建 — 自动安装                    |
| `agent/tools/code_intel/lsp/fallback.py`  | 新建 — fallback 链                 |
| `agent/tools/code_intel/lsp/client.py`    | 修改 — 用 resolver 发现二进制      |
| `agent/tools/code_intel/lsp/tools.py`     | 修改 — 每个工具调用前检查可用性    |

### Phase 2X 修改（2 个）

| 文件                                          | 修改                                   |
| --------------------------------------------- | -------------------------------------- |
| `config/features/agent_side/lsp.py`           | 扩展 `lsp_supported_servers` (+6 语言) |
| `agent/tools/code_intel/lsp/protocol.py`      | 追加 rename/diagnostics 数据结构       |
| `agent/tools/code_intel/lsp/tools.py`         | 追加 4 个工具                          |
| `agent/tools/subagent/spawn/core.py`          | 注入新工具                             |
| `agent/tools/subagent/spawn/system_prompt.py` | 追加 LSP 工具指导                      |

### Phase 4 修改（2 个）

| 文件                                                       | 修改           |
| ---------------------------------------------------------- | -------------- |
| `agent/tools/subagent/spawn/role_definitions/librarian.py` | 新建           |
| `agent/tools/subagent/spawn/role_definitions/__init__.py`  | 注册 librarian |

### Phase 5 修改（3 个）

| 文件                                      | 修改                  |
| ----------------------------------------- | --------------------- |
| `pyproject.toml`                          | 加 `watchfiles>=2.0`  |
| `agent/tools/code_intel/watcher.py`       | 新建                  |
| `server/__main__.py` 或 `server/service/` | 启动 watcher 后台任务 |

### 公共修改

| 文件             | 修改                                                       |
| ---------------- | ---------------------------------------------------------- |
| `config/path.py` | 加 `CODE_INTEL_DIR = ROOT_DIR / ".codeintel"` (原计划已有) |
| `.gitignore`     | 加 `.codeintel/` (原计划已有)                              |

---

## 测试计划（补充）

| 文件                                                        | 阶段 | 标记          | 覆盖点                                                                                  |
| ----------------------------------------------------------- | ---- | ------------- | --------------------------------------------------------------------------------------- |
| `tests/agent/tools/code_intel/ast_grep/test_resolver.py`    | P1A  | `unit`        | 5 层发现（env/runtime/skill-bin/PATH/homebrew）、--version 探针、缓存命中、Windows 后缀 |
| `tests/agent/tools/code_intel/ast_grep/test_provisioner.py` | P1A  | `unit`        | SHA-256 校验、ZIP 解压、6 平台 asset、下载失败/校验失败/写入失败                        |
| `tests/agent/tools/code_intel/ast_grep/test_runner.py`      | P1A  | `unit`        | sg 子进程调用、pattern 解析、元变量匹配、dry_run rewrite、超时、binary 未找到 fallback  |
| `tests/agent/tools/code_intel/lsp/test_resolver.py`         | P2S  | `unit`        | 三层发现（显式路径/repo-local/PATH）、缓存命中、Windows 后缀、marker-gated bin 目录     |
| `tests/agent/tools/code_intel/lsp/test_installer.py`        | P2S  | `unit`        | 自动安装命令执行、超时、returncode、安装后重发现、auto_install=False 拒绝               |
| `tests/agent/tools/code_intel/lsp/test_fallback.py`         | P2S  | `unit`        | available/not_installed/not_configured 三态、fallback 消息格式、语言检测                |
| `tests/agent/tools/code_intel/lsp/test_lsp_tools.py`        | P2S  | `integration` | 4 LSP 工具端到端 + not_installed fallback 路径                                          |
| `tests/agent/tools/code_intel/lsp/test_lsp_extended.py`     | P2X  | `integration` | lsp_rename / lsp_diagnostics / lsp_format / lsp_status                                  |
| `tests/agent/tools/subagent/test_librarian_role.py`         | P4   | `integration` | librarian 角色定义加载、工具权限正确（有 explore/无 write）                             |
| `tests/agent/tools/code_intel/test_watcher.py`              | P5   | `integration` | 文件变更触发重索引、debounce 2s、prune_dirs 排除                                        |

### 关键测试用例

```python
# test_resolver.py
def test_resolve_env_override():
    """SHERRY_SG_PATH 环境变量指向的路径优先于其他层。"""

def test_resolve_runtime_dir():
    """~/.sherry/runtime/ast-grep/<slug>/sg 被探测。"""

def test_resolve_skill_bin():
    """skills/ast-grep/bin/sg 被探测。"""

def test_resolve_from_path():
    """PATH 上的 ast-grep / sg 被发现。"""

def test_resolve_homebrew():
    """Homebrew 前缀目录被探测。"""

def test_version_probe_rejects_impostor():
    """--version 输出不含 'ast-grep' 的二进制被拒绝。"""

def test_resolve_not_found():
    """所有层均未命中时返回 None。"""

def test_resolve_cache_hit():
    """相同参数的第二次调用命中缓存。"""

# test_provisioner.py
def test_sha256_mismatch_rejected():
    """下载内容 SHA-256 不匹配时返回 None。"""

def test_zip_extraction():
    """从 ZIP 中正确提取 sg/ast-grep 二进制。"""

def test_download_failure():
    """下载失败时返回 None，不崩溃。"""

def test_write_permission():
    """写入 ~/.sherry/runtime/ 失败时返回 None。"""

def test_provision_then_resolve():
    """provision 后 resolve_sg_binary() 能发现新二进制。"""

# test_runner.py
def test_search_success():
    """正常 pattern 返回匹配结果。"""

def test_search_binary_not_found():
    """二进制未找到时返回安装提示。"""

def test_rewrite_dry_run():
    """dry_run=True 时不修改文件。"""

def test_rewrite_apply():
    """dry_run=False 时实际修改文件。"""

# test_resolver.py (LSP)
def test_resolve_local_venv_bin():
    """pyproject.toml 存在时 .venv/bin 被探测。"""

def test_resolve_local_node_modules():
    """package.json 存在时 node_modules/.bin 被探测。"""

def test_resolve_from_path():
    """PATH 上的二进制被发现。"""

def test_resolve_not_installed():
    """二进制不存在时返回 None。"""

def test_resolve_cache_hit():
    """相同参数的第二次调用命中缓存，不重复探测文件系统。"""

def test_marker_gate():
    """无 marker 文件时 bin 目录不被信任。"""

# test_installer.py (LSP)
def test_auto_install_disabled():
    """lsp_auto_install=False 时返回安装提示而非执行安装。"""

def test_auto_install_timeout():
    """安装超时返回明确错误。"""

def test_auto_install_success():
    """安装成功后 resolve_lsp_server 能发现新二进制。"""

# test_fallback.py (LSP)
def test_not_installed_returns_hint():
    """LSP 未安装时返回安装提示 + fallback 建议。"""

def test_not_configured_returns_list():
    """语言不在 enabled list 时返回 not_configured。"""

def test_available_returns_binary():
    """LSP 已安装时返回 available + binary path。"""
```

---

## 安全清单（补充）

| 维度                                       | 措施                                                               | 阶段 |
| ------------------------------------------ | ------------------------------------------------------------------ | ---- |
| **ast-grep 二进制 provision SHA-256 校验** | 下载的 ZIP 经 SHA-256 校验后才解压，防止供应链篡改                 | P1A  |
| **ast-grep 二进制版本锁定**                | pinned 0.43.0，不从 PATH 接受任意版本，--version 探针验证          | P1A  |
| **ast-grep 子进程隔离**                    | sg CLI 通过 subprocess.run 启动，有超时（30s），不继承敏感环境变量 | P1A  |
| **ast-grep rewrite dry_run**               | 默认 dry_run=True，必须显式设置 false 才修改文件                   | P1A  |
| **LSP 子进程环境清洗**                     | LSP server 子进程环境经 `scrub_env()` 清洗，不泄漏 API keys        | P2S  |
| **LSP 自动安装用户授权**                   | `lsp_auto_install` 默认 False，需用户在配置中显式开启              | P2S  |
| **LSP 安装命令白名单**                     | 仅 `lsp_auto_install_commands` 中列出的命令可执行，不接受任意命令  | P2S  |
| **LSP 安装超时**                           | 60s 超时，防止安装命令挂起                                         | P2S  |
| **LSP 二进制路径可信**                     | resolve_local 仅在 marker 文件存在时信任 bin 目录，防止目录注入    | P2S  |
| **文件监听排除敏感目录**                   | watcher 排除 prune_dirs（.git, .venv, node_modules, ...）          | P5   |
| **文件监听仅触发索引**                     | watcher 不执行代码，仅 parse + 写 SQLite                           | P5   |
| **librarian 临时仓库清理**                 | clone 到 temp 目录，subagent 结束时清理                            | P4   |
| **librarian 只读约束**                     | tool_deny 包含 write/edit/patch_file，无法修改文件                 | P4   |
| **librarian 无 spawn 权限**                | tool_deny 包含 sessions_spawn/yield/send，无法创建子 subagent      | P4   |

---

## 关键参考文件

### Sherry 内部（复用）

| 文件                                    | 用途                                                               |
| --------------------------------------- | ------------------------------------------------------------------ |
| `agent/tools/pub_base/env_scrub.py`     | `scrub_env()` — LSP/sg 子进程环境清洗                              |
| `agent/tools/file_tools/search_scan.py` | `bounded_walk` + `ScanState` — 索引遍历复用                        |
| `agent/tools/web_search.py`             | `build_web_search_tool()` — librarian 复用                         |
| `agent/tools/terminal.py`               | terminal 工具 — librarian git/gh 操作复用                          |
| `agent/tools/file_tools/read_file.py`   | `build_read_file_tool()` — librarian 源码读取复用                  |
| `context_engine/embeddings/store.py`    | Embedding BLOB 存储（Phase 3 复用，原计划已有）                    |
| `config/path.py` `ROOT_DIR`             | ast-grep skill bin cache 路径基础 (`ROOT_DIR/skills/ast-grep/bin`) |

### 外部参考

| 项目                  | 文件                                         | 参考内容                                                                      |
| --------------------- | -------------------------------------------- | ----------------------------------------------------------------------------- |
| `oh-my-openagent-dev` | `lsp-core/src/lsp/server-installation.ts`    | 三层二进制发现（repo-local → PATH → Windows 后缀）                            |
| `oh-my-openagent-dev` | `lsp-core/src/lsp/server-definitions.ts`     | `AUTO_INSTALLABLE_SERVERS` + `LSP_INSTALL_HINTS` + `LSP_LOCAL_INSTALL_HINTS`  |
| `oh-my-openagent-dev` | `lsp-core/src/lsp/server-resolution.ts`      | `findServerForExtension()` not_installed 状态 + installHint 返回              |
| `oh-my-openagent-dev` | `lsp-core/src/tools/install-decision.ts`     | `lsp_install_decision` 工具（用户授权安装）                                   |
| `oh-my-openagent-dev` | `lsp-core/src/tools/status.ts`               | `lsp_status` 工具（服务器状态聚合）                                           |
| `oh-my-openagent-dev` | `ast-grep-mcp/src/tools/search.ts`           | sg CLI 参数构建 + 输出解析                                                    |
| `oh-my-openagent-dev` | `ast-grep-mcp/src/tools/rewrite.ts`          | dry_run rewrite 实现                                                          |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-resolver.ts`          | 5 层二进制发现（env/runtime/skill-bin/PATH/homebrew）+ --version 探针         |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-provisioner.ts`       | ast-grep 二进制 SHA-256 验证下载 + ZIP 解压                                   |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-candidates.ts`        | 5 层候选路径生成逻辑                                                          |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-install-hints.ts`     | 平台特定的安装提示（brew/npm/cargo/scoop/winget/choco）                       |
| `oh-my-openagent-dev` | `utils/src/ast-grep/sg-manifest.ts`          | pinned 版本号 + 6 平台 SHA-256 校验值                                         |
| `oh-my-openagent-dev` | `utils/src/ast-grep/install-script.ts`       | install.sh / install.ps1 启动器（30s 超时, Windows pwsh→powershell fallback） |
| `oh-my-openagent-dev` | `shared-skills/skills/ast-grep/install.sh`   | 286 行 7 路 fallback 安装脚本（brew/npm/cargo/pip/nix/mise/github）           |
| `oh-my-openagent-dev` | `omo-senpi/src/components/ast-grep/index.ts` | ast-grep MCP server 注册（enabled: true, lifecycle: "lazy"）                  |
| `oh-my-openagent-dev` | `omo-senpi/src/extension/component-list.ts`  | ast-grep 无条件注册（与 LSP 并列）                                            |
| `oh-my-openagent-dev` | `agents/librarian.ts`                        | librarian system prompt + 工具权限                                            |
| `oh-my-openagent-dev` | `utils/src/codegraph/provision.ts`           | 二进制 auto-provisioning 模式（SHA-256 验证）                                 |
| `deepagents-main`     | `libs/code/deepagents_code/managed_tools.py` | managed ripgrep 自动安装 + SHA-256 验证模式                                   |
