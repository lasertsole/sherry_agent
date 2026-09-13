"""Unit tests for the infra-side feature configuration registry.

Covers ``config/features/infra_side/``:
(a) every instance's keys exactly match its ``TypedDict`` annotations;
(b) spot-checked default values per feature;
(c) ``_build_gateway`` env injection and loopback defaults;
(d) collection-shaped fields (dict / frozenset / tuple / list) keep their type.
"""

import pytest

from config.features import infra_side as fs
from config.features.infra_side.gateway import _build_gateway

pytestmark = [pytest.mark.unit]

# (name, instance, TypedDict, spot-checked defaults; GATEWAY is env-sourced and
# exercised by the dedicated builder tests instead).
CASES: list[tuple[str, object, object, dict[str, object]]] = [
    ("BUS", fs.BUS, fs.BusConfig, {"queue_maxsize": 1000}),
    (
        "HTTP_UPLOAD",
        fs.HTTP_UPLOAD,
        fs.HttpUploadConfig,
        {
            "max_image_bytes": 25 * 1024 * 1024,
            "max_audio_bytes": 100 * 1024 * 1024,
            "max_video_bytes": 500 * 1024 * 1024,
        },
    ),
    (
        "RETRY_BACKOFF",
        fs.RETRY_BACKOFF,
        fs.RetryBackoffConfig,
        {
            "jittered_base_delay": 2.0,
            "jittered_max_delay": 60.0,
            "jittered_jitter": 0.3,
            "backoff_floor": 0.1,
            "adaptive_base_delay": 5.0,
            "adaptive_max_delay": 120.0,
        },
    ),
    (
        "SERVER_HTTP",
        fs.SERVER_HTTP,
        fs.ServerHttpConfig,
        {
            "log_dir": "/logs/output",
            "media_dir_name": "media",
            "turn_page_size": 200,
            "audio_default_ext": ".mp3",
            "image_default_ext": ".png",
            "video_default_ext": ".mp4",
            "memory_max_content_length": 8000,
        },
    ),
    (
        "WS_STREAM",
        fs.WS_STREAM,
        fs.WsStreamConfig,
        {
            "max_continuation_retries": 4,
            "max_reasoning_only_retries": 2,
            "drain_error_backoff_s": 1.0,
            "max_flatten_depth": 5,
        },
    ),
    (
        "INPUT_QUEUE",
        fs.INPUT_QUEUE,
        fs.InputQueueConfig,
        {
            "busy_timeout_ms": 5000,
            "init_wait_timeout_s": 10.0,
            "max_active_per_session": 20,
            "expiry_seconds": 86400.0,
            "lock_sweep_threshold": 256,
        },
    ),
    (
        "HEARTBEAT_SERVICE",
        fs.HEARTBEAT_SERVICE,
        fs.HeartbeatServiceConfig,
        {
            "ws_session_id": "default",
            "heartbeat_file_name": "HEARTBEAT.md",
            "max_content_length": 2000,
            "interval_s": 1800,
            "backoff_factor": 2.0,
            "backoff_max_interval_s": 7200.0,
            "backoff_max_consecutive_failures": 5,
        },
    ),
    (
        "CRON_SERVICE",
        fs.CRON_SERVICE,
        fs.CronServiceConfig,
        {
            "min_every_ms": 1000,
            "max_run_history": 20,
            "degraded_threshold": 5,
            "disabled_threshold": 10,
            "degrade_backoff_base_ms": 5000,
            "degrade_backoff_max_ms": 300000,
            "ws_session_id": "default",
        },
    ),
    (
        "SKILL_SCANNER",
        fs.SKILL_SCANNER,
        fs.SkillScannerConfig,
        {
            "cli_timeout": 120,
            "cli_timeout_env_var": "SKILL_SCANNER_TIMEOUT",
            "cache_version": 1,
            "exit_ok": 0,
            "exit_do_not_install": 1,
            "exit_error": 2,
            "fail_closed_on_do_not_install": True,
        },
    ),
    (
        "MES_MEMORY",
        fs.MES_MEMORY,
        fs.MesMemoryConfig,
        {
            "busy_timeout_s": 10.0,
            "connect_attempts": 5,
            "retry_delay_s": 0.2,
            "max_query_tokens": 64,
            "max_token_chars": 64,
            "max_wildcard_terms": 4,
        },
    ),
    (
        "CURATOR_DEFAULTS",
        fs.CURATOR_DEFAULTS,
        fs.CuratorDefaultsConfig,
        {
            "default_interval_hours": 120,
            "default_min_idle_hours": 2,
            "default_stale_after_days": 30,
            "default_archive_after_days": 90,
            "default_consolidate": False,
            "interval_override_min_days": 1,
            "interval_override_max_days": 5,
            "http_interval_min_days": 1,
            "http_interval_max_days": 5,
        },
    ),
    (
        "MESSAGE_PIPELINE",
        fs.MESSAGE_PIPELINE,
        fs.MessagePipelineConfig,
        {
            "slice_last_turn_token_max": 6000,
            "tool_output_dedup_default_protected_tools": frozenset(),
        },
    ),
    (
        "PERIODIC_BACKOFF",
        fs.PERIODIC_BACKOFF,
        fs.PeriodicBackoffConfig,
        {"factor": 2.0, "max_interval_s": 7200.0, "max_consecutive_failures": 5},
    ),
    (
        "CRASH_LOOP",
        fs.CRASH_LOOP,
        fs.CrashLoopConfig,
        {"window_s": 300, "trip_threshold": 3, "retention_s": 3600, "reason_max_len": 200},
    ),
    (
        "SKILLS_TOOLING",
        fs.SKILLS_TOOLING,
        fs.SkillsToolingConfig,
        {
            "speech_daemon_host": "127.0.0.1",
            "speech_daemon_port": 9011,
            "speech_ready_wait_seconds": 8.0,
            "speech_liveness_timeout_seconds": 1.0,
            "speech_http_timeout_seconds": 60.0,
            "speech_server_host": "127.0.0.1",
            "speech_server_port": 9011,
            "video_min_duration_sec": 0.0,
            "video_max_duration_sec": 60.0,
            "skill_creator_max_skill_name_length": 64,
            "wiki_subdir": "wiki",
        },
    ),
    (
        "CHANNELS",
        fs.CHANNELS,
        fs.ChannelsConfig,
        {"dep_install_timeout_seconds": 120},
    ),
]


def test_all_seventeen_features_present() -> None:
    # 16 data-driven cases plus GATEWAY, which is env-sourced and covered by
    # the dedicated builder tests below.
    assert len(CASES) + 1 == 17


@pytest.mark.parametrize(("name", "instance", "typed_dict", "_specimen"), CASES)
def test_keys_match_annotations(
    name: str, instance: object, typed_dict: object, _specimen: dict
) -> None:
    assert set(instance) == set(typed_dict.__annotations__), name


@pytest.mark.parametrize(("name", "instance", "typed_dict", "specimen"), CASES)
def test_spot_defaults(name: str, instance: dict, typed_dict: object, specimen: dict) -> None:
    expected_checks = min(3, len(typed_dict.__annotations__))
    assert len(specimen) >= expected_checks, f"{name} needs more spot-checks"
    for key, expected in specimen.items():
        assert instance[key] == expected, f"{name}.{key}"


class TestGatewayBuilder:
    """``GATEWAY`` is env-sourced; verify both injection and defaults."""

    def test_injected_env_values(self) -> None:
        built = _build_gateway({"API_HOST": "0.0.0.0", "API_PORT": "9999"})
        assert (built["api_host"], built["api_port"]) == ("0.0.0.0", 9999)

    def test_empty_env_uses_loopback_defaults(self) -> None:
        built = _build_gateway({})
        assert (built["api_host"], built["api_port"]) == ("127.0.0.1", 8080)

    def test_gateway_keys_match_annotations(self) -> None:
        assert set(fs.GATEWAY) == set(fs.GatewayConfig.__annotations__)


class TestCollectionFieldTypes:
    """Map / frozenset / tuple / list fields keep their declared container type."""

    def test_maps_are_dicts(self) -> None:
        assert isinstance(fs.SERVER_HTTP["audio_content_type_to_ext"], dict)
        assert isinstance(fs.SERVER_HTTP["image_content_type_to_ext"], dict)
        assert isinstance(fs.SERVER_HTTP["video_content_type_to_ext"], dict)
        assert isinstance(fs.SERVER_HTTP["skills_disk_to_category"], dict)

    def test_map_spot_members(self) -> None:
        assert fs.SERVER_HTTP["audio_content_type_to_ext"]["audio/mpeg"] == ".mp3"
        assert fs.SERVER_HTTP["image_content_type_to_ext"]["image/jpeg"] == ".jpg"
        assert fs.SERVER_HTTP["video_content_type_to_ext"]["video/mp4"] == ".mp4"
        assert fs.SERVER_HTTP["skills_disk_to_category"]["plugins"] == "third_party"

    def test_frozensets_are_frozensets(self) -> None:
        assert isinstance(fs.SERVER_HTTP["knowledge_graph_allowed_ext"], frozenset)
        assert isinstance(fs.SERVER_HTTP["skills_skip_dirs"], frozenset)
        assert isinstance(fs.SERVER_HTTP["skills_skip_suffixes"], frozenset)
        assert isinstance(fs.SERVER_HTTP["env_split_out_keys"], frozenset)
        assert isinstance(
            fs.MESSAGE_PIPELINE["tool_output_dedup_default_protected_tools"], frozenset
        )

    def test_frozenset_members(self) -> None:
        assert fs.SERVER_HTTP["knowledge_graph_allowed_ext"] == frozenset(
            {".pdf", ".docx", ".txt", ".md"}
        )
        assert fs.SERVER_HTTP["skills_skip_dirs"] == frozenset(
            {"__pycache__", ".git", ".venv", "node_modules"}
        )
        assert fs.SERVER_HTTP["skills_skip_suffixes"] == frozenset({".pyc", ".pyo"})
        assert "LANGSMITH_API_KEY" in fs.SERVER_HTTP["env_split_out_keys"]
        assert fs.MESSAGE_PIPELINE["tool_output_dedup_default_protected_tools"] == frozenset()

    def test_tuples_are_tuples(self) -> None:
        assert isinstance(fs.SERVER_HTTP["subagent_public_fields"], tuple)
        assert isinstance(fs.SERVER_HTTP["env_group_prefixes"], tuple)
        assert isinstance(fs.WS_STREAM["stream_diag_headers"], tuple)
        assert isinstance(fs.HEARTBEAT_SERVICE["main_llm_env_vars"], tuple)

    def test_list_field_is_list(self) -> None:
        assert isinstance(fs.SERVER_HTTP["memory_system_file_names"], list)
        assert fs.SERVER_HTTP["memory_system_file_names"] == ["MEMORY.md", "USER.md"]


class TestPackageAggregator:
    """The package must re-export both registry halves from one place."""

    def test_reexports_both_halves(self) -> None:
        import config.features as features

        for name in ("SUMMARIZATION", "MODEL_BACKEND", "GATEWAY", "CRON_SERVICE", "CHANNELS"):
            assert hasattr(features, name), name
