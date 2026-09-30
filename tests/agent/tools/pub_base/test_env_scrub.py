"""Tests for agent.tools.pub_base.env_scrub.scrub_env.

Behavior contract (sandbox-hardening spec):
- Vars whose NAME contains a secret substring (case-insensitive) are dropped.
- Critical vars are kept by exact name or by name prefix so child processes
  keep working (PATH lookup, Windows loader, etc.).
- sherry_agent's own secret vars are dropped by exact name.
- Precedence: hijack-block > name-keep > name-deny > substring-block.

Rule tables are the source of truth; tests use only fake variable names
(no real secrets).
"""

import os

import pytest

from agent.tools.pub_base.env_scrub import SHERRY_SECRET_NAMES, scrub_env
from agent.tools.pub_base import env_scrub


pytestmark = [pytest.mark.module]


class TestSecretSubstringFiltered:
    """Vars containing secret substrings in their name are dropped."""

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("SHERRY_TEST_SECRET", "s3cret"),
            ("DB_PASSWORD", "hunter2"),
            ("AUTH_TOKEN", "tok123"),
            ("SERVICE_DSN", "postgres://x"),
            ("MY_BEARER", "b64"),
            ("CALLBACK_WEBHOOK", "https://hook"),
            ("DEPLOY_PASSWD", "pw"),
            ("AWS_CREDENTIAL", "cred"),
        ],
    )
    def test_dropped(self, name, value):
        result = scrub_env({name: value})
        assert name not in result

    def test_apikey_contiguous_substring(self):
        """Contiguous APIKEY substring is blocked (covers GOOGLE-style names)."""
        result = scrub_env({"GOOGLE_APIKEY": "abc", "PLAIN_VAR": "ok"})
        assert "GOOGLE_APIKEY" not in result
        assert result == {"PLAIN_VAR": "ok"}


class TestCaseInsensitive:
    """Substring matching is case-insensitive."""

    @pytest.mark.parametrize(
        "name",
        ["db_PaSsWoRd", "lowercase_secret", "MixedCase_ToKeN", "lower_auth"],
    )
    def test_mixed_case_dropped(self, name):
        assert name not in scrub_env({name: "x"})


class TestPrefixKeep:
    """Name-prefix keeps survive substring blocking (LC_, XDG_, CONDA)."""

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("LC_ALL", "en_US.UTF-8"),
            ("XDG_CACHE_HOME", "/tmp/xdg"),
            ("CONDA_PREFIX", "C:/conda"),
        ],
    )
    def test_prefix_kept(self, name, value):
        result = scrub_env({name: value})
        assert result == {name: value}

    def test_prefix_keep_beats_substring_block(self):
        """Precedence: name-keep > substring-block.

        CONDA prefix keep wins over the TOKEN substring in CONDA_TOKEN.
        """
        result = scrub_env({"CONDA_TOKEN": "x", "LC_MESSAGE_KEY": "y"})
        assert result == {"CONDA_TOKEN": "x", "LC_MESSAGE_KEY": "y"}


class TestNamePrecedence:
    """Precedence documentation via KEY_PATH_DELIM (plan QA scenario).

    KEY_PATH_DELIM contains the substring KEY. The keep rule matches EXACT
    names only (PATH, HOME, ...), not arbitrary names containing PATH, so
    KEY_PATH_DELIM is NOT name-kept and the substring rule drops it.
    This behavior is asserted explicitly here.
    """

    def test_key_path_delim_is_substring_blocked(self):
        assert "KEY_PATH_DELIM" not in scrub_env({"KEY_PATH_DELIM": ":"})

    def test_exact_name_keep_is_immune_to_substring_block(self):
        """Exact-name keep (PATH) wins over substring logic by construction."""
        assert scrub_env({"PATH": "C:/bin"}) == {"PATH": "C:/bin"}

    def test_name_deny_beats_everything(self):
        """Precedence: name-deny drops sherry keys even though KEY substring
        would drop them anyway; deny list is the explicit contract."""
        env = {name: "fake-value" for name in SHERRY_SECRET_NAMES}
        env["SAFE_VAR"] = "ok"
        result = scrub_env(env)
        assert not any(name in result for name in SHERRY_SECRET_NAMES)
        assert result == {"SAFE_VAR": "ok"}


class TestSherryKeysFiltered:
    """All 11 sherry_agent secret names are force-filtered by exact name."""

    def test_all_sherry_keys_absent(self):
        env = {name: "fake-value" for name in SHERRY_SECRET_NAMES}
        result = scrub_env(env)
        assert result == {}

    def test_sherry_key_among_clean_vars(self):
        env = {
            "MAIN_LLM_API_KEY": "fake",
            "TAVILY_API_KEY": "fake",
            "EDITOR": "vim",
        }
        result = scrub_env(env)
        assert "MAIN_LLM_API_KEY" not in result
        assert "TAVILY_API_KEY" not in result
        assert result == {"EDITOR": "vim"}


class TestBaseEnvHandling:
    """base_env argument semantics."""

    def test_none_defaults_to_os_environ(self, monkeypatch):
        monkeypatch.setenv("SHERRY_TEST_SECRET", "x")
        result = scrub_env()
        assert "SHERRY_TEST_SECRET" not in result
        assert bool(result.get("PATH")), "PATH must survive os.environ scrub"

    def test_empty_env_returns_empty(self):
        assert scrub_env({}) == {}

    def test_input_dict_not_mutated(self):
        base = {"SHERRY_TEST_SECRET": "x", "EDITOR": "vim"}
        snapshot = dict(base)
        scrub_env(base)
        assert base == snapshot


class TestWindowsCriticalPreserved:
    """Windows loader/lookup vars survive scrubbing."""

    def test_windows_vars_kept(self):
        env = {
            "SystemRoot": "C:/Windows",
            "COMSPEC": "C:/Windows/system32/cmd.exe",
            "PATH": "C:/Windows/system32",
            "PATHEXT": ".COM;.EXE",
            "APPDATA": "C:/Users/x/AppData/Roaming",
            "LOCALAPPDATA": "C:/Users/x/AppData/Local",
            "USERPROFILE": "C:/Users/x",
            "SYSTEMDRIVE": "C:",
            "NUMBER_OF_PROCESSORS": "8",
            "PROCESSOR_ARCHITECTURE": "AMD64",
            "COMPUTERNAME": "PC",
            "OS": "Windows_NT",
            "WINDIR": "C:/Windows",
            "HOMEDRIVE": "C:",
            "HOMEPATH": "C:/Users/x",
        }
        result = scrub_env(env)
        assert result == env

    def test_unix_vars_kept(self):
        env = {
            "HOME": "/home/x",
            "USER": "x",
            "USERNAME": "x",
            "LANG": "en_US.UTF-8",
            "TERM": "xterm-256color",
            "TMPDIR": "/tmp",
            "TMP": "C:/temp",
            "TEMP": "C:/temp",
            "SHELL": "/bin/bash",
            "LOGNAME": "x",
            # PYTHONPATH moved to the hijack block (see TestHijackVectors): the
            # one child that needs one sets it after scrubbing.
            "PYTHONUTF8": "1",
            "VIRTUAL_ENV": "/repo/.venv",
        }
        result = scrub_env(env)
        assert result == env


class TestCleanVarsPassThrough:
    def test_clean_vars_untouched(self):
        env = {"EDITOR": "vim", "SHERRY_MODE": "1", "RUST_LOG": "debug"}
        assert scrub_env(env) == env


class TestOsEnvironIntegration:
    def test_real_environ_scrub_on_real_module_tree(self, monkeypatch):
        """Import under the real module tree (pub_base __init__ runs) and
        scrub the live os.environ copy."""
        monkeypatch.setenv("SHERRY_TEST_SECRET", "leak")
        monkeypatch.setenv("MAIN_LLM_API_KEY", "leak")
        result = scrub_env(dict(os.environ))
        assert "SHERRY_TEST_SECRET" not in result
        assert "MAIN_LLM_API_KEY" not in result
        assert "PATH" in result


class TestHijackVectors:
    """Startup-hook variables never reach a child process.

    These hijack the *child's* runtime rather than merely naming a secret: a
    poisoned ``PYTHONPATH`` makes it import attacker modules, ``BASH_ENV`` runs a
    script before every non-interactive shell. They are blocked at a precedence
    above keep-by-name so no future keep entry can re-open the hole.
    """

    HIJACK_VARS = (
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONHOME",
        "BASH_ENV",
        "ENV",
        "ZDOTDIR",
        "PERL5OPT",
        "RUBYOPT",
    )

    @pytest.mark.parametrize("name", HIJACK_VARS)
    def test_hijack_vars_are_dropped(self, name):
        assert name not in scrub_env({name: "/tmp/attacker", "PATH": "/usr/bin"})

    def test_the_hijack_block_outranks_the_keep_list(self):
        """``PYTHONPATH`` used to be keep-listed; the block must still win."""
        env = scrub_env({"PYTHONPATH": "/tmp/attacker", "PYTHONUTF8": "1"})

        assert "PYTHONPATH" not in env
        assert env["PYTHONUTF8"] == "1", "the rest of the Python runtime vars stay"

    def test_hijack_matching_is_case_insensitive(self):
        assert "pythonpath" not in scrub_env({"pythonpath": "/tmp/attacker"})
        assert "PyThOnPaTh" not in scrub_env({"PyThOnPaTh": "/tmp/attacker"})

    def test_ordinary_and_critical_vars_are_unaffected(self):
        env = scrub_env({"PATH": "/usr/bin", "HOME": "/root", "MY_TOOL": "1", "LC_ALL": "C.UTF-8"})

        assert env == {"PATH": "/usr/bin", "HOME": "/root", "MY_TOOL": "1", "LC_ALL": "C.UTF-8"}

    def test_input_is_still_not_mutated(self):
        source = {"BASH_ENV": "/tmp/attacker", "PATH": "/usr/bin"}

        scrub_env(source)

        assert source == {"BASH_ENV": "/tmp/attacker", "PATH": "/usr/bin"}


class TestLoaderVariables:
    """The dynamic loader's variables follow the platform, so they are opt-in.

    Measured on this development host: it exports ``LD_PRELOAD`` and
    ``LD_LIBRARY_PATH`` for its container runtime, and stripping them makes every
    child fail to start (the programmatic-tool-calling suite times out). Blocking
    them by default would break the platform to close a vector, so they move
    behind ``SHERRY_STRICT_ENV_HIJACK`` for hosts that own their loader
    environment.
    """

    LOADER_VARS = ("LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH")

    @pytest.mark.parametrize("name", LOADER_VARS)
    def test_loader_vars_survive_by_default(self, name, monkeypatch):
        monkeypatch.setattr(env_scrub, "_STRICT_ENV_HIJACK", False, raising=False)

        assert name in scrub_env({name: "/platform/loader", "PATH": "/usr/bin"})

    @pytest.mark.parametrize("name", LOADER_VARS)
    def test_loader_vars_are_dropped_in_strict_mode(self, name, monkeypatch):
        monkeypatch.setattr(env_scrub, "_STRICT_ENV_HIJACK", True)

        assert name not in scrub_env({name: "/tmp/attacker", "PATH": "/usr/bin"})

    def test_strict_mode_keeps_the_hijack_block(self, monkeypatch):
        monkeypatch.setattr(env_scrub, "_STRICT_ENV_HIJACK", True)

        env = scrub_env({"PYTHONPATH": "/tmp/attacker", "PATH": "/usr/bin"})

        assert env == {"PATH": "/usr/bin"}
