"""Unit tests for server.trigger.http.channels._channel_icon_url.

The icon resolver must always pick exactly ONE file out of a directory that
may contain many images, in this priority order:
  1. explicit ``icon`` key in plugins/channels/<name>/config.json
  2. convention file ``<name>_icon_128.<ext>``
  3. lexicographically first file in the icon dir
"""

import pytest

from server.trigger.http import channels as ch


HOST, PORT = "127.0.0.1", "9999"


@pytest.fixture
def icon_env(tmp_path, monkeypatch):
    """Point the resolver at a temp plugins tree so real files are never touched."""
    monkeypatch.setattr(ch, "PLUGINS_PATH", tmp_path)
    monkeypatch.setattr(ch, "API_HOST", HOST)
    monkeypatch.setattr(ch, "API_PORT", PORT)
    configs = {}

    def fake_config(name):
        return configs.get(name, {})

    monkeypatch.setattr(ch, "_load_channel_local_config", fake_config)

    def icon_dir(channel_name="qq"):
        d = tmp_path / "channels" / channel_name / "icon"
        d.mkdir(parents=True, exist_ok=True)
        return d

    return {"icon_dir": icon_dir, "configs": configs, "tmp": tmp_path}


def _url(icon_env, file_name, channel="qq"):
    return f"http://{HOST}:{PORT}/channels/{channel}/icon/{file_name}"


def test_multi_icon_picks_convention_file(icon_env):
    """Multiple icons + no explicit config -> canonical {name}_icon_128.png wins."""
    d = icon_env["icon_dir"]()
    (d / "a.png").write_bytes(b"1")
    (d / "qq_icon_128.png").write_bytes(b"2")
    (d / "z.png").write_bytes(b"3")
    assert ch._channel_icon_url("qq") == _url(icon_env, "qq_icon_128.png")


def test_multi_icon_picks_lex_first_without_convention(icon_env):
    """Multiple icons, no convention file -> lexicographically first file wins."""
    d = icon_env["icon_dir"]()
    (d / "zz_active.png").write_bytes(b"1")
    (d / "aa_default.png").write_bytes(b"2")
    assert ch._channel_icon_url("qq") == _url(icon_env, "aa_default.png")


def test_explicit_config_icon_overrides_convention(icon_env):
    """'icon' key in per-channel config beats the convention file."""
    d = icon_env["icon_dir"]()
    (d / "qq_icon_128.png").write_bytes(b"1")
    (d / "featured.png").write_bytes(b"2")
    icon_env["configs"]["qq"] = {"icon": "featured.png"}
    assert ch._channel_icon_url("qq") == _url(icon_env, "featured.png")


def test_explicit_icon_missing_falls_back(icon_env):
    """Explicit icon that does not exist -> convention file is used instead."""
    d = icon_env["icon_dir"]()
    (d / "qq_icon_128.png").write_bytes(b"1")
    (d / "other.png").write_bytes(b"2")
    icon_env["configs"]["qq"] = {"icon": "does_not_exist.png"}
    assert ch._channel_icon_url("qq") == _url(icon_env, "qq_icon_128.png")


def test_explicit_icon_traversal_is_basename_only(icon_env):
    """A path-traversal icon value degrades to its filename and stays inside dir."""
    d = icon_env["icon_dir"]()
    (d / "safe.png").write_bytes(b"1")
    icon_env["configs"]["qq"] = {"icon": "../../evil.png"}
    # ".name" strips the traversal; it must not match (file absent) and must
    # not escape the icon dir. Result: fall through to the only real file.
    assert ch._channel_icon_url("qq") == _url(icon_env, "safe.png")


def test_no_icon_dir_returns_empty(icon_env):
    """Absent icon directory -> empty string, no error."""
    assert ch._channel_icon_url("qq") == ""


def test_empty_icon_dir_returns_empty(icon_env):
    """Present but empty icon directory -> empty string."""
    icon_env["icon_dir"]()
    assert ch._channel_icon_url("qq") == ""


def test_svg_convention_is_also_supported(icon_env):
    """Convention matching honors the svg extension too."""
    d = icon_env["icon_dir"]()
    (d / "qq_icon_128.svg").write_bytes(b"<svg/>")
    (d / "aa.png").write_bytes(b"1")
    assert ch._channel_icon_url("qq") == _url(icon_env, "qq_icon_128.svg")


def test_channel_name_targets_own_icon_dir(icon_env):
    """Icon lookup is scoped to the channel's own icon dir, not a shared one."""
    d = icon_env["icon_dir"]("qq")
    other = icon_env["icon_dir"]("other")
    (d / "qq_icon_128.png").write_bytes(b"1")
    (other / "qq_icon_128.png").write_bytes(b"2")
    icon_env["configs"]["other"] = {"icon": "qq_icon_128.png"}
    # 'qq' resolves inside its own dir; 'other' also inside its own dir.
    assert ch._channel_icon_url("qq") == _url(icon_env, "qq_icon_128.png", "qq")
    assert ch._channel_icon_url("other") == _url(icon_env, "qq_icon_128.png", "other")
