"""Environment variable scrubbing before child process spawn.

``scrub_env`` builds a safe environment dictionary for child processes
(terminal / python_repl spawn points). It is a pure function: no IO, no
logging, no third-party imports (only ``os`` / ``re``).

Filtering rules (source of truth: ``docs/sandbox/README.md``):

- Substring block (case-insensitive match on the variable NAME): vars whose
  name contains a secret-ish substring (KEY, TOKEN, ...) are dropped.
- Keep by exact name / name prefix: vars child processes need to function
  (PATH lookup, Windows loader, locale, temp dirs, ...). Not an allowlist —
  anything not matching any rule below passes through untouched.
- Deny by exact name: sherry_agent's own secret vars loaded from ``.env`` are
  always dropped.

Precedence: keep-by-name > deny-by-name > substring-block.

Deliberately NOT implemented: allowlist-only mode (dropping PATH breaks child
processes), dynamic secret detection, and logging of filtered variable names
(avoid leaking secret names into logs).
"""

import os
import re

__all__ = ["SHERRY_SECRET_NAMES", "scrub_env"]

# Substrings that indicate a secret. Matched case-insensitively against the
# variable NAME. Note: sherry's own keys use ``API_KEY`` (with underscore),
# which does NOT contain the contiguous substring ``APIKEY`` — hence the
# explicit deny-name list below.
_SECRET_SUBSTRINGS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "PASSWD",
    "AUTH",
    "DSN",
    "WEBHOOK",
    "BEARER",
    "APIKEY",
)

# Vars kept by EXACT name (case-insensitive). Exact match only — a name that
# merely CONTAINS one of these (e.g. KEY_PATH_DELIM) is not name-kept.
_KEEP_EXACT_NAMES = frozenset(
    {
        # POSIX / shell
        "PATH",
        "HOME",
        "USER",
        "USERNAME",
        "LANG",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SHELL",
        "LOGNAME",
        # Python runtime
        "PYTHONUTF8",
        "VIRTUAL_ENV",
        # Windows OS essentials (missing these breaks child processes)
        "COMPUTERNAME",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "OS",
        "PROCESSOR_ARCHITECTURE",
        "NUMBER_OF_PROCESSORS",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
    }
)

# Vars kept by NAME PREFIX (case-insensitive).
_KEEP_NAME_PREFIXES = ("LC_", "XDG_", "CONDA")

# Variables that let whoever set them run code in the child — or redirect where
# it loads code from — before the child's own code runs.
#
# Two tiers, because one of them is platform-owned:
#
# * ``_HIJACK_KEYS`` are dropped always. They are interpreter/shell startup
#   hooks: nothing legitimate needs to hand the agent's children a startup file.
# * ``_LOADER_KEYS`` are dropped only under ``SHERRY_STRICT_ENV_HIJACK=1``.
#   They are the dynamic loader's own variables, and container tooling sets them
#   for real: this development host (PRoot/Android) exports both ``LD_PRELOAD``
#   and ``LD_LIBRARY_PATH``, and stripping them makes every child process fail to
#   start — measured, the programmatic-tool-calling suite times out with them
#   blocked and passes 63/63 without. A hardening step that bricks the children
#   is worse than the vector it closes, so the strict tier exists for deployments
#   that control their own loader environment.
#
# ``PYTHONPATH`` stays in the always-dropped tier, verified rather than assumed:
# nothing in this repository sets it, the project is not installed into the venv
# (the server and agent run as ``python -m server`` from the repository root,
# which puts the cwd on ``sys.path``), the skill scripts add the repository root
# themselves with ``sys.path.insert``, and the one child that needs a
# ``PYTHONPATH`` — the programmatic-tool-calling runner, for its ``sherry_tools``
# stub directory — sets it right after scrubbing
# (``ptc/runner.py::build_child_env``), overwriting whatever it inherited.
_HIJACK_KEYS = frozenset(
    {
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "BASH_ENV",
        "ENV",
        "ZDOTDIR",
        "PERL5OPT",
        "RUBYOPT",
    }
)

#: Loader variables, dropped only in strict mode (see the note above).
_LOADER_KEYS = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
    }
)

#: Strict mode for hosts whose loader environment is under the operator's control.
_STRICT_ENV_HIJACK = os.getenv("SHERRY_STRICT_ENV_HIJACK", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _blocked_env_names() -> frozenset[str]:
    """The hijack set in force for this process (strict mode adds the loaders)."""
    return _HIJACK_KEYS | _LOADER_KEYS if _STRICT_ENV_HIJACK else _HIJACK_KEYS


# sherry_agent secret vars (loaded from .env at import time via
# config/path.py load_dotenv), dropped by EXACT name.
SHERRY_SECRET_NAMES = frozenset(
    {
        "MAIN_LLM_API_KEY",
        "TAVILY_API_KEY",
        "LANGSMITH_API_KEY",
        "REASONER_LLM_API_KEY",
        "AUXILIARY_LLM_API_KEY",
        "ITTT_API_KEY",
        "VTTT_API_KEY",
        "TTI_API_KEY",
        "RERANKER_API_KEY",
        "EMBEDDING_API_KEY",
        "STT_API_KEY",
    }
)

_SUBSTRING_PATTERN = re.compile("|".join(_SECRET_SUBSTRINGS), re.IGNORECASE)
_SECRET_NAMES_UPPER = {name.upper() for name in SHERRY_SECRET_NAMES}
_KEEP_PREFIXES_UPPER = tuple(prefix.upper() for prefix in _KEEP_NAME_PREFIXES)


def _is_kept(upper_name: str) -> bool:
    """Name-keep rule: exact name or prefix match (case-insensitive)."""
    return upper_name in _KEEP_EXACT_NAMES or upper_name.startswith(_KEEP_PREFIXES_UPPER)


def scrub_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment dict safe to pass to a child process.

    Filters, by variable NAME only (values are never inspected):

    1. Hijack-block wins over everything: a startup-hook variable
       (``PYTHONPATH``, ``BASH_ENV``, ``ENV``, …) never reaches a child, not even
       when a keep rule would have kept it. In ``SHERRY_STRICT_ENV_HIJACK=1``
       mode the loader variables (``LD_PRELOAD``, ``LD_LIBRARY_PATH``, …) join it.
    2. Keep-by-name/prefix comes next (critical vars such as ``PATH``,
       ``SystemRoot``, ``LC_*`` survive even when the name also matches a
       blocking rule).
    3. Deny-by-name drops sherry_agent's own secret vars.
    4. Substring-block drops any remaining name containing a secret
       substring (KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL, PASSWD, AUTH,
       DSN, WEBHOOK, BEARER, APIKEY — case-insensitive).

    Everything else passes through unchanged.

    Args:
        base_env: Environment to scrub. ``None`` scrubs a copy of the current
            process environment (``os.environ``).

    Returns:
        A new dict; ``base_env`` is never mutated.
    """
    source: dict[str, str] = os.environ if base_env is None else base_env
    blocked = _blocked_env_names()
    safe_env: dict[str, str] = {}
    for name, value in source.items():
        upper_name = name.upper()
        # Precedence: hijack-block > keep-by-name > deny-by-name > substring-block.
        # The hijack check runs FIRST on purpose: it must win over the keep list
        # (and over any future entry someone adds there).
        if upper_name in blocked:
            continue
        if _is_kept(upper_name):
            safe_env[name] = value
        elif upper_name in _SECRET_NAMES_UPPER:
            continue
        elif _SUBSTRING_PATTERN.search(name):
            continue
        else:
            safe_env[name] = value
    return safe_env
