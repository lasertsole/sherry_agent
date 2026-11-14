"""Three-layer LSP server configuration: builtin + skill lsp.json + project lsp.json.

The builtin table (``LSP["lsp_supported_servers"]``) stays the code-owned base.
Two declarative layers extend it without touching code:

* **skill** — ``<skill_dir>/lsp.json`` next to a ``SKILL.md`` (builtin / auto /
  plugins roots). A skill only DECLARES a server; the binary is still resolved
  by the existing five-tier ``resolver`` and never shipped by the skill.
* **project** — ``<project>/.sherry/lsp.json``, editable/uploadable from the
  系统配置 → LSP tab. Precedence: project > skill > builtin, whole-spec
  replacement (no half-merged field sets).

Every declaration is validated before it can reach ``subprocess``: a bare
executable name (no path separators, no shell metacharacters), shell-free argv,
dot-prefixed extensions and bounded sizes. Invalid entries are dropped and
reported (fail-open: the other entries still load). Reads are mtime-cached so a
project file edit applies on the next query without a restart.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import json5
from loguru import logger

from config import SKILL_DISCOVERY_ROOTS, SKILLS_DIR
from config.features import LSP
from config.path import resolve_default_project_dir

__all__ = [
    "LspConfigView",
    "effective_enabled_languages",
    "effective_lsp_servers",
    "load_lsp_payload",
    "project_config_path",
    "project_payload",
    "read_project_config",
    "skill_lsp_servers",
    "validate_server_spec",
    "write_project_config",
]

PROJECT_LSP_RELATIVE = Path(".sherry") / "lsp.json"
MAX_FILE_BYTES = 64 * 1024
MAX_SERVERS = 100
MAX_COMMAND_ITEMS = 8
MAX_EXTENSIONS = 16
MAX_COMMAND_CHARS = 256
MAX_EXTENSION_CHARS = 16
MAX_LANGUAGE_ID_CHARS = 64
MAX_HINT_CHARS = 500

_SHELL_META = re.compile(r"[;&|<>`$\n\r]")
_LANGUAGE_KEY = re.compile(r"^[A-Za-z0-9_+#.-]{1,32}$")
_EXECUTABLE = re.compile(r"^[A-Za-z0-9._+-]{1,128}$")
_EXTENSION = re.compile(r"^\.[A-Za-z0-9_+-]{1,15}$")

_LOCK = threading.RLock()
_PROJECT_CACHE: dict[str, tuple[int, dict[str, Any], list[str], list[str]]] = {}
_SKILL_CACHE: dict[
    str,
    tuple[tuple[tuple[str, int, int], ...], dict[str, Any], dict[str, str], list[str], list[str]],
] = {}


@dataclass
class LspConfigView:
    """The merged view the tools and the config API consume."""

    servers: dict[str, dict[str, Any]]
    sources: dict[str, str]
    enabled: list[str]
    errors: list[str] = field(default_factory=list)

    def source_of(self, language: str) -> str:
        return self.sources.get(language, "builtin")


def _spec_copy(spec: dict[str, Any]) -> dict[str, Any]:
    copied = dict(spec)
    copied["command"] = list(spec.get("command") or [])
    copied["extensions"] = list(spec.get("extensions") or [])
    return copied


def _errors_path(label: str, message: str) -> str:
    return f"{label}: {message}"


def validate_server_spec(
    language: str, raw: object, *, label: str = ""
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate one declared server; return ``(clean_spec, errors)``.

    ``command[0]`` must be a BARE executable name: a declaration cannot point at
    an arbitrary absolute path (the five-tier resolver — explicit builtin path,
    marker-gated repo bin, runtime dir, PATH, Homebrew — is the only place a
    concrete binary is chosen).
    """
    name = label or f"server '{language}'"
    errors: list[str] = []
    if not _LANGUAGE_KEY.fullmatch(str(language or "")):
        return None, [f"invalid language key {language!r}"]
    if not isinstance(raw, dict):
        return None, [f"{name}: spec must be an object"]
    command = raw.get("command")
    if not isinstance(command, (list, tuple)) or not command:
        return None, [f"{name}: command must be a non-empty list"]
    if len(command) > MAX_COMMAND_ITEMS:
        return None, [f"{name}: command has more than {MAX_COMMAND_ITEMS} items"]
    clean_command: list[str] = []
    for index, item in enumerate(command):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{name}: command[{index}] must be a non-empty string")
            continue
        if len(item) > MAX_COMMAND_CHARS:
            errors.append(f"{name}: command[{index}] is too long")
            continue
        if _SHELL_META.search(item):
            errors.append(f"{name}: command[{index}] contains a shell metacharacter")
            continue
        if index == 0 and not _EXECUTABLE.fullmatch(item):
            errors.append(
                f"{name}: command[0] must be a bare executable name "
                "(letters, digits, . _ + -; no path separators)"
            )
            continue
        clean_command.append(item)
    if len(clean_command) != len(command):
        return None, errors
    if not _EXECUTABLE.fullmatch(clean_command[0]):
        return None, errors + [f"{name}: command[0] is not a valid executable name"]

    raw_extensions = raw.get("extensions")
    if not isinstance(raw_extensions, (list, tuple)) or not raw_extensions:
        return None, errors + [f"{name}: extensions must be a non-empty list"]
    if len(raw_extensions) > MAX_EXTENSIONS:
        return None, errors + [f"{name}: more than {MAX_EXTENSIONS} extensions"]
    clean_extensions: list[str] = []
    for extension in raw_extensions:
        text = str(extension or "").strip()
        if not _EXTENSION.fullmatch(text) or len(text) > MAX_EXTENSION_CHARS:
            errors.append(f"{name}: invalid extension {extension!r}")
            continue
        clean_extensions.append(text)
    if len(clean_extensions) != len(raw_extensions):
        return None, errors

    language_id = str(raw.get("language_id") or language).strip()
    if not language_id or len(language_id) > MAX_LANGUAGE_ID_CHARS:
        return None, errors + [f"{name}: language_id must be 1-{MAX_LANGUAGE_ID_CHARS} chars"]

    spec: dict[str, Any] = {
        "command": clean_command,
        "extensions": clean_extensions,
        "language_id": language_id,
        "local_install": None,
    }
    for key in ("install_hint", "local_install"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > MAX_HINT_CHARS:
            errors.append(f"{name}: {key} must be a string up to {MAX_HINT_CHARS} chars")
            continue
        spec[key] = value
    # An unknown key is ignored (forward compatibility), but a declaration we
    # had to repair is refused rather than half-applied.
    return (None, errors) if errors else (spec, [])


def load_lsp_payload(
    payload: object, *, label: str = "lsp.json"
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Parse one lsp.json object into ``(servers, enable, errors)``."""
    if not isinstance(payload, dict):
        return {}, [], [f"{label}: root must be an object"]
    raw_servers = payload.get("servers")
    if raw_servers is None:
        raw_servers = {}
    if not isinstance(raw_servers, dict):
        return {}, [], [f"{label}: 'servers' must be an object"]
    servers: dict[str, Any] = {}
    errors: list[str] = []
    for language, raw_spec in list(raw_servers.items())[:MAX_SERVERS]:
        clean, problems = validate_server_spec(str(language), raw_spec, label=f"{label} {language}")
        errors.extend(problems)
        if clean is not None:
            servers[str(language)] = clean
    if len(raw_servers) > MAX_SERVERS:
        errors.append(f"{label}: more than {MAX_SERVERS} servers")
    raw_enable = payload.get("enable")
    enable: list[str] = []
    if raw_enable is not None:
        if not isinstance(raw_enable, list):
            errors.append(f"{label}: 'enable' must be a list of language keys")
        else:
            for value in raw_enable:
                text = str(value or "").strip()
                if _LANGUAGE_KEY.fullmatch(text):
                    enable.append(text)
                else:
                    errors.append(f"{label}: invalid enabled language {value!r}")
    return servers, list(dict.fromkeys(enable)), errors


def _read_json_file(path: Path, *, label: str) -> tuple[object | None, list[str]]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None, [f"{label}: file exceeds {MAX_FILE_BYTES} bytes"]
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"{label}: cannot read ({exc})"]
    try:
        return json5.loads(text), []
    except ValueError as exc:
        return None, [f"{label}: invalid JSON ({exc})"]


def project_config_path(root: Path | str | None = None) -> Path:
    base = Path(root).resolve() if root is not None else resolve_default_project_dir()
    return base / PROJECT_LSP_RELATIVE


def read_project_config(
    root: Path | str | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """The project layer ``(servers, enable, errors)`` with an mtime cache."""
    path = project_config_path(root)
    try:
        stat = path.stat()
        key = str(path)
        with _LOCK:
            cached = _PROJECT_CACHE.get(key)
        if cached is not None and cached[0] == stat.st_mtime_ns:
            return dict(cached[1]), list(cached[2]), list(cached[3])
    except OSError:
        return {}, [], []
    payload, errors = _read_json_file(path, label=f"project {path.name}")
    if payload is None:
        return {}, [], errors
    servers, enable, parse_errors = load_lsp_payload(payload, label="project lsp.json")
    result = (servers, enable, errors + parse_errors)
    try:
        with _LOCK:
            _PROJECT_CACHE[str(path)] = (path.stat().st_mtime_ns, servers, enable, result[2])
    except OSError:
        pass
    return result


def project_payload(root: Path | str | None = None) -> dict[str, Any]:
    """The raw project lsp.json as the editor receives it (empty when absent)."""
    servers, enable, _errors = read_project_config(root)
    return {"servers": servers, "enable": enable}


def skill_lsp_servers() -> tuple[dict[str, Any], dict[str, str], list[str], list[str]]:
    """Merge every ``<skill_dir>/lsp.json`` under the allowed skill roots."""
    files: list[Path] = []
    try:
        for path in SKILLS_DIR.rglob("lsp.json"):
            relative = path.relative_to(SKILLS_DIR)
            if not relative.parts or relative.parts[0] not in SKILL_DISCOVERY_ROOTS:
                continue
            if not (path.parent / "SKILL.md").is_file():
                continue
            files.append(path)
    except OSError:
        return {}, {}, [], []
    files.sort()
    try:
        signature = tuple(
            (str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in files
        )
    except OSError:
        return {}, {}, [], []
    key = "skills"
    with _LOCK:
        cached = _SKILL_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return dict(cached[1]), dict(cached[2]), list(cached[3]), list(cached[4])

    servers: dict[str, Any] = {}
    sources: dict[str, str] = {}
    enable: list[str] = []
    errors: list[str] = []
    for path in files:
        payload, read_errors = _read_json_file(path, label=f"skill {path.parent.name}")
        errors.extend(read_errors)
        if payload is None:
            continue
        file_servers, file_enable, file_errors = load_lsp_payload(
            payload, label=f"skill {path.parent.name}"
        )
        errors.extend(file_errors)
        source = f"skill:{path.parent.name}"
        for language, spec in file_servers.items():
            servers[language] = spec
            sources[language] = source
        enable.extend(file_enable)
    result = (servers, sources, list(dict.fromkeys(enable)), errors)
    with _LOCK:
        _SKILL_CACHE[key] = (signature, servers, sources, result[2], errors)
    return result


def effective_lsp_servers(root: Path | str | None = None) -> LspConfigView:
    """Merge builtin → skill → project (later layers replace whole specs)."""
    servers: dict[str, Any] = {
        language: _spec_copy(spec) for language, spec in LSP["lsp_supported_servers"].items()
    }
    sources = {language: "builtin" for language in servers}
    enabled = list(LSP["lsp_enabled_languages"])
    errors: list[str] = []

    skill_servers, skill_sources, skill_enable, skill_errors = skill_lsp_servers()
    for language, spec in skill_servers.items():
        servers[language] = _spec_copy(spec)
        sources[language] = skill_sources.get(language, "skill")
    enabled.extend(skill_enable)
    errors.extend(skill_errors)

    project_servers, project_enable, project_errors = read_project_config(root)
    for language, spec in project_servers.items():
        servers[language] = _spec_copy(spec)
        sources[language] = "project"
    enabled.extend(project_enable)
    errors.extend(project_errors)

    return LspConfigView(
        servers=servers,
        sources=sources,
        enabled=list(dict.fromkeys(enabled)),
        errors=errors,
    )


def effective_enabled_languages(root: Path | str | None = None) -> list[str]:
    """The merged ``lsp_enabled_languages`` list (builtin + declared enables)."""
    return effective_lsp_servers(root).enabled


def write_project_config(
    root: Path | str | None, payload: object
) -> tuple[LspConfigView | None, list[str]]:
    """Validate and atomically write ``<root>/.sherry/lsp.json``.

    Returns ``(view, errors)``; nothing is written when validation fails. The
    project directory itself is created here (the user's explicit save is the
    only trigger — startup never writes it).
    """
    from pub.func.atomic_replace import atomic_replace

    servers, enable, errors = load_lsp_payload(payload, label="project lsp.json")
    if errors:
        return None, errors
    path = project_config_path(root)
    body = json.dumps({"servers": servers, "enable": enable}, ensure_ascii=False, indent=2)
    if len(body.encode("utf-8")) > MAX_FILE_BYTES:
        return None, [f"project lsp.json exceeds {MAX_FILE_BYTES} bytes"]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(body + "\n", encoding="utf-8")
        atomic_replace(temporary, path)
    except OSError as exc:
        return None, [f"cannot write {path}: {exc}"]
    with _LOCK:
        _PROJECT_CACHE.pop(str(path), None)
    logger.info("LSP project config written: {} ({} server(s))", path, len(servers))
    return effective_lsp_servers(root), []
