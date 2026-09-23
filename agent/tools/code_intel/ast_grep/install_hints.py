"""ast-grep install hints — returned to the caller when the binary is missing.

Mirrors oh-my-openagent's sg-install-hints.ts. Hints are platform-specific and
always end with the two sherry-native recovery paths (auto-provision and the
``SHERRY_SG_PATH`` override) so a caller with a broken install has an
actionable next step instead of a dead tool.
"""

import sys

_SHERRY_PROVISION_HINT = (
    "Or let sherry provision the pinned runtime automatically on the next call "
    "(downloads the pinned release and SHA-256-verifies it)"
)
_ENV_OVERRIDE_HINT = "Or point SHERRY_SG_PATH at an existing ast-grep binary"

_DARWIN_HINTS = [
    "brew install ast-grep",
    "npm install -g @ast-grep/cli",
    "cargo install ast-grep --locked",
]

_LINUX_HINTS = [
    "npm install -g @ast-grep/cli",
    "cargo install ast-grep --locked",
    "pip install ast-grep-cli",
    "brew install ast-grep  # linuxbrew",
]

_WIN32_HINTS = [
    "scoop install main/ast-grep",
    "winget install ast-grep",
    "choco install ast-grep",
    "npm install -g @ast-grep/cli",
]


def sg_install_hints(platform: str = sys.platform) -> list[str]:
    """Return actionable install hints for *platform* (defaults to the host)."""
    if platform == "darwin":
        base = _DARWIN_HINTS
    elif platform == "win32":
        base = _WIN32_HINTS
    else:
        base = _LINUX_HINTS
    return [*base, _SHERRY_PROVISION_HINT, _ENV_OVERRIDE_HINT]


def sg_binary_not_found_message(platform: str = sys.platform) -> str:
    """Return the human-readable "binary unavailable" message."""
    return (
        f"ast-grep binary not found for {platform}: no candidate passed the "
        f"--version probe across the env override, sherry runtime, skill bin "
        f"cache, PATH, or Homebrew prefixes."
    )
