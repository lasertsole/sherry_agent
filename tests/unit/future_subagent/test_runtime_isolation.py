import pytest
from future_subagent.spawn.runtime_isolation import (
    RuntimeIsolationConfig,
    resolve_runtime_isolation,
    validate_runtime_isolation,
    validate_cwd_restriction,
)


class TestRuntimeIsolationConfig:
    def test_defaults(self):
        cfg = RuntimeIsolationConfig()
        assert cfg.runtime == "subagent"
        assert cfg.allowed_cwd_prefixes == []
        assert cfg.restricted is False

    def test_custom(self):
        cfg = RuntimeIsolationConfig(
            runtime="custom",
            allowed_cwd_prefixes=["/workspace"],
            restricted=True,
        )
        assert cfg.runtime == "custom"
        assert cfg.allowed_cwd_prefixes == ["/workspace"]
        assert cfg.restricted is True


class TestResolveRuntimeIsolation:
    def test_default_main(self):
        cfg = resolve_runtime_isolation("agent:main:session:p1")
        assert cfg.runtime == "subagent"
        assert cfg.restricted is False

    def test_cross_runtime_restricted(self):
        cfg = resolve_runtime_isolation("agent:main:session:p1", agent_id="other_runtime")
        assert cfg.restricted is True

    def test_same_runtime_not_restricted(self):
        cfg = resolve_runtime_isolation("agent:main:session:p1", agent_id="subagent")
        assert cfg.restricted is False

    def test_with_cwd(self):
        cfg = resolve_runtime_isolation("agent:main:session:p1", cwd="/workspace/project")
        assert cfg.allowed_cwd_prefixes == ["/workspace/project"]


class TestValidateRuntimeIsolation:
    def test_not_restricted(self):
        cfg = RuntimeIsolationConfig()
        ok, reason = validate_runtime_isolation(cfg)
        assert ok is True
        assert reason == ""

    def test_restricted(self):
        cfg = RuntimeIsolationConfig(restricted=True)
        ok, reason = validate_runtime_isolation(cfg)
        assert ok is False
        assert "Cross-runtime" in reason


class TestValidateCwdRestriction:
    def test_no_cwd(self):
        ok, _ = validate_cwd_restriction(None, ["/workspace"])
        assert ok is True

    def test_empty_prefixes(self):
        ok, _ = validate_cwd_restriction("/some/path", [])
        assert ok is True

    def test_cwd_within_prefix(self):
        ok, _ = validate_cwd_restriction("/workspace/project", ["/workspace"])
        assert ok is True

    def test_cwd_outside_prefix(self):
        ok, reason = validate_cwd_restriction("/etc/passwd", ["/workspace"])
        assert ok is False
        assert "outside" in reason

    def test_multiple_prefixes(self):
        ok, _ = validate_cwd_restriction("/data/files", ["/workspace", "/data"])
        assert ok is True

    def test_no_matching_prefix(self):
        ok, _ = validate_cwd_restriction("/tmp/evil", ["/workspace", "/data"])
        assert ok is False
