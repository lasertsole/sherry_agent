"""SkillSpector scanner limits, exit codes and environment flags."""

from typing import TypedDict


class SkillScannerConfig(TypedDict):
    """SkillSpector scanner limits, exit codes and environment flags."""

    cli_timeout: int
    cli_timeout_env_var: str
    version_probe_timeout: int
    cache_version: int
    cache_path: str
    exit_ok: int
    exit_do_not_install: int
    exit_error: int
    fail_closed_on_do_not_install: bool
    enabled_env_var: str
    llm_enabled_env_var: str


SKILL_SCANNER: SkillScannerConfig = {
    "cli_timeout": 120,
    "cli_timeout_env_var": "SKILL_SCANNER_TIMEOUT",
    "version_probe_timeout": 10,
    "cache_version": 1,
    "cache_path": "src/data/skills_scan_cache.json",
    "exit_ok": 0,
    "exit_do_not_install": 1,
    "exit_error": 2,
    "fail_closed_on_do_not_install": True,
    "enabled_env_var": "SKILL_SCANNER_ENABLED",
    "llm_enabled_env_var": "SKILL_SCANNER_LLM",
}
