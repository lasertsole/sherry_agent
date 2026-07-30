"""Hardline blocklist and dangerous-pattern detection for shell commands.

Two-tier detection:
1. **Hardline** (:data:`HARDLINE_PATTERNS`) — unconditional block; no bypass possible.
2. **Dangerous** (:data:`DANGEROUS_PATTERNS`) — matched patterns that require human approval.

All functions are **pure** — no state, no side effects, no LangChain dependency.
"""

from __future__ import annotations

import re

# ── Hardline blocklist (unconditional, no bypass) ──────────────────────

HARDLINE_PATTERNS: list[re.Pattern[str]] = [
    # Matches: rm -rf /, rm -rf --no-preserve-root, rm -rf C:\
    # Blocked unconditionally — system-destroying operations.
    re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+(/|[A-Z]:\\)|--no-preserve-root)", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=.*of=/dev/", re.IGNORECASE),
    re.compile(r"\b(sudo\s+)?shutdown\b", re.IGNORECASE),
    re.compile(r"\b(sudo\s+)?reboot\b", re.IGNORECASE),
    re.compile(r":()\s*\{\s*:\s*\|\s*:&\s*\}", re.IGNORECASE),
    re.compile(r"\bfork\s*\(?\s*\)\s*\{", re.IGNORECASE),
    re.compile(r"\bchmod\s+(-R\s+)?777\s+/", re.IGNORECASE),
    re.compile(r"\bchown\s+(-R\s+)?\S+\s+/", re.IGNORECASE),
    re.compile(r">\s*/dev/sd[a-z]", re.IGNORECASE),
    re.compile(r"\bsysctl\s+-w\s", re.IGNORECASE),
    re.compile(r"\biptables\s+-F\b", re.IGNORECASE),
]  # fmt: skip

# ── Dangerous pattern detection (47+ patterns) ────────────────────────

# DANGEROUS_PATTERNS: 47+ regex/tag pairs.
# Each entry is (compiled_regex, human_readable_tag).
# Tags are used in ApprovalResult.pattern_key for allowlist tracking.
DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*)", re.IGNORECASE), "rm_recursive_force"),
    (re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*|-[a-zA-Z]*r[a-zA-Z]*)\s+", re.IGNORECASE), "rm_recursive"),
    (re.compile(r"\bchmod\s+(000|777|666)\b", re.IGNORECASE), "chmod_dangerous"),
    (re.compile(r"\bchown\s+(-R\s+)?.*\s+/", re.IGNORECASE), "chown_recursive_root"),
    (re.compile(r"\bgit\s+push\s+(--force|-f)\b", re.IGNORECASE), "git_force_push"),
    (re.compile(r"\bgit\s+push\s+.*--delete\b", re.IGNORECASE), "git_delete_remote"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE), "git_reset_hard"),
    (re.compile(r"\bgit\s+clean\s+-fdx\b", re.IGNORECASE), "git_clean_force"),
    (re.compile(r"\bgit\s+checkout\s+--\s*\.\s*$", re.IGNORECASE), "git_checkout_discard_all"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE), "sql_drop"),
    (re.compile(r"\bTRUNCATE\s+TABLE?\b", re.IGNORECASE), "sql_truncate"),
    (re.compile(r"\bDELETE\s+FROM\b", re.IGNORECASE), "sql_delete_no_where"),
    (re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE), "sql_alter"),
    (re.compile(r"\bcurl\s+.*\|\s*(ba)?sh\b", re.IGNORECASE), "curl_pipe_sh"),
    (re.compile(r"\bwget\s+.*\|\s*(ba)?sh\b", re.IGNORECASE), "wget_pipe_sh"),
    (re.compile(r"\b(?:npm|npx|yarn)\s+publish\b", re.IGNORECASE), "npm_publish"),
    (re.compile(r"\bdocker\s+(rm|rmi)\s+(-f|--force)", re.IGNORECASE), "docker_force_remove"),
    (re.compile(r"\bdocker\s+system\s+prune\s+(-a|--all)", re.IGNORECASE), "docker_prune_all"),
    (re.compile(r"\bkubectl\s+delete\s+", re.IGNORECASE), "kubectl_delete"),
    (re.compile(r"\bpip\s+uninstall\s+", re.IGNORECASE), "pip_uninstall"),
    (re.compile(r"\bsudo\s+rm\b", re.IGNORECASE), "sudo_rm"),
    (re.compile(r"\bsudo\s+chmod\b", re.IGNORECASE), "sudo_chmod"),
    (re.compile(r"\bsudo\s+chown\b", re.IGNORECASE), "sudo_chown"),
    (re.compile(r"\bsudo\s+tee\b", re.IGNORECASE), "sudo_tee_overwrite"),
    (re.compile(r"\bmv\s+.*\s+/dev/null\b", re.IGNORECASE), "mv_dev_null"),
    (re.compile(r"\bcp\s+.*\s+/dev/null\b", re.IGNORECASE), "cp_dev_null"),
    (re.compile(r"\bsystemctl\s+(stop|disable|restart)\s+", re.IGNORECASE), "systemctl_stop"),
    (re.compile(r"\bservice\s+\w+\s+stop\b", re.IGNORECASE), "service_stop"),
    (re.compile(r"\bkill\s+(-9|-KILL)\s+", re.IGNORECASE), "kill_9"),
    (re.compile(r"\bkillall\s+", re.IGNORECASE), "killall"),
    (re.compile(r"\bpkill\s+(-9|-KILL)?\s+", re.IGNORECASE), "pkill"),
    (re.compile(r"\btaskkill\s+(/F|/IM)\b", re.IGNORECASE), "taskkill_force"),
    (re.compile(r"\breg(?:edit)?\s+", re.IGNORECASE), "registry_edit"),
    (re.compile(r"\bformat\s+[A-Z]:\\", re.IGNORECASE), "format_drive"),
    (re.compile(r"\bnet\s+(user|localgroup)\s+", re.IGNORECASE), "net_user_modify"),
    (re.compile(r"\bschtasks\s+/(create|delete|change)\b", re.IGNORECASE), "scheduled_task_modify"),
    (re.compile(r"\bpowercfg\b", re.IGNORECASE), "power_config"),
    (re.compile(r"\bbcdedit\b", re.IGNORECASE), "boot_config"),
    (re.compile(r"\bDISM\b", re.IGNORECASE), "dism_modify"),
    (re.compile(r"\bsfc\s+/scannow\b", re.IGNORECASE), "system_file_check"),
    (re.compile(r"\bwbadmin\s+", re.IGNORECASE), "backup_admin"),
    (re.compile(r"\bdiskpart\b", re.IGNORECASE), "disk_partition"),
    (re.compile(r"\bSet-ExecutionPolicy\b", re.IGNORECASE), "ps_execution_policy"),
    (re.compile(r"\bInvoke-WebRequest\b", re.IGNORECASE), "ps_web_request"),
    (re.compile(r"\bStart-Process\b", re.IGNORECASE), "ps_start_process"),
    (re.compile(r"\bRemove-Item\s+(-Recurse|-Force)", re.IGNORECASE), "ps_remove_item"),
    (re.compile(r"\bStop-Process\s+-Force\b", re.IGNORECASE), "ps_stop_process"),
    (re.compile(r"\bnetsh\b", re.IGNORECASE), "network_shell"),
    (re.compile(r"\bapt\s+(remove|purge)\s+", re.IGNORECASE), "apt_remove"),
    (re.compile(r"\byum\s+remove\s+", re.IGNORECASE), "yum_remove"),
    (re.compile(r"\bpacman\s+-R\s+", re.IGNORECASE), "pacman_remove"),
    (re.compile(r"\bsnap\s+remove\s+", re.IGNORECASE), "snap_remove"),
    (re.compile(r"\bENV\s+\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)\s*=", re.IGNORECASE), "env_secret_set"),
    (re.compile(r"\bexport\s+\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)\s*=", re.IGNORECASE), "export_secret"),
]


def detect_hardline_command(command: str) -> str | None:
    """Check if *command* matches any hardline blocklist pattern.

    Hardline patterns are **unconditionally** denied — no bypass is possible.

    Args:
        command: The shell command string to inspect.

    Returns:
        A pattern tag like ``"hardline:\\\\brm\\\\s+..."`` if matched, or ``None``.
    """
    for pattern in HARDLINE_PATTERNS:
        if pattern.search(command):
            return f"hardline:{pattern.pattern}"
    return None


def detect_dangerous_command(command: str) -> list[tuple[str, str]]:
    """Find all dangerous patterns present in *command*.

    Each match is returned as ``(regex_pattern, tag)``. Multiple patterns
    may match a single command (e.g. both ``sudo_rm`` and ``rm_recursive_force``).

    Args:
        command: The shell command string to inspect.

    Returns:
        List of ``(pattern.pattern, tag)`` tuples for every matched pattern.
        Empty list means the command appears safe.
    """
    matches: list[tuple[str, str]] = []
    for pattern, tag in DANGEROUS_PATTERNS:
        if pattern.search(command):
            matches.append((pattern.pattern, tag))
    return matches
