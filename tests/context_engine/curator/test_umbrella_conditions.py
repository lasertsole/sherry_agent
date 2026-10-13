"""The umbrella skill's two-level use conditions (frontmatter + sub-capabilities).

`when_to_use` is REQUIRED on every skill the curator writes; `when_not_to_use`
is optional and omitted when there is genuinely no exclusion. The body carries a
second, narrower level: every `##` sub-capability section opens with its own
`**When to use:**` line so the model can pick the right section after loading
the umbrella with `skill_view`.
"""

from __future__ import annotations

import pytest

from context_engine.curator import umbrella as umbrella_mod

pytestmark = [pytest.mark.unit]


_REASONS = [
    "password-hashing: use when authentication needs non-blocking bcrypt verification. "
    "Alternative: argon2 projects.",
    "jwt-signing: use when stateless token auth is needed. Not for session-based auth.",
    "oauth: for third-party login (Google/GitHub).",
]


class TestFallbackUmbrella:
    def test_frontmatter_carries_when_to_use_and_omits_an_absent_exclusion(self):
        content = umbrella_mod._fallback_umbrella(
            "plain-tools", ["only-skill: a capability with no exclusions."], "BODY"
        )

        assert "when_to_use: " in content
        assert "when_not_to_use:" not in content, "optional — no tag/field without a real exclusion"

    def test_frontmatter_carries_both_when_both_are_extractable(self):
        content = umbrella_mod._fallback_umbrella("async-auth", _REASONS, "BODY")

        header = content.split("---", 2)[1]
        assert "when_to_use:" in header
        assert "when_not_to_use:" in header

    def test_every_sub_capability_opens_with_its_own_when_to_use(self):
        content = umbrella_mod._fallback_umbrella("async-auth", _REASONS, "BODY")

        # Each merged SUB-CAPABILITY opens with its own trigger line; the
        # umbrella-level "## When to use" / "## When NOT to use" sections are
        # the conditions themselves and are exempt.
        for name in ("password-hashing", "jwt-signing", "oauth"):
            block = content.split(f"## {name}", 1)[1]
            block = block.split("## ", 1)[0]
            assert "**When to use:**" in block, block[:80]

    def test_a_sub_capability_with_an_exclusion_gets_the_not_block(self):
        content = umbrella_mod._fallback_umbrella("async-auth", _REASONS, "BODY")

        jwt_section = content.split("## jwt-signing", 1)[1].split("## oauth", 1)[0]
        assert "**When NOT to use:** Not for session-based auth" in jwt_section

    def test_a_sub_capability_without_an_exclusion_omits_the_not_block(self):
        content = umbrella_mod._fallback_umbrella("async-auth", _REASONS, "BODY")

        oauth_section = content.split("## oauth", 1)[1]
        assert "**When NOT to use:**" not in oauth_section

    def test_conditions_are_token_bounded(self):
        long_reason = "skill-x: use when " + ("x" * 2000) + "."

        content = umbrella_mod._fallback_umbrella("huge", [long_reason], "BODY")

        first_line = next(line for line in content.split("\n") if line.startswith("when_to_use: "))
        assert len(first_line) < 400, "a 50-token budget must bound the condition line"

    def test_the_umbrella_level_sections_elaborate_the_frontmatter(self):
        content = umbrella_mod._fallback_umbrella("async-auth", _REASONS, "BODY")

        assert "## When to use" in content
        assert "## When NOT to use" in content
        assert "Consolidated source content:" in content and "BODY" in content


class _FakeLlm:
    """Captures the system message the umbrella generator sends."""

    def __init__(self):
        self.system = ""

    def invoke(self, messages):
        self.system = messages[0].content

        class _Response:
            content = "---\nname: x\ndescription: d\n---\nbody"

        return _Response()


class TestGeneratorPrompt:
    def test_the_prompt_requires_when_to_use_and_allows_omitting_the_exclusion(self, monkeypatch):
        fake = _FakeLlm()
        monkeypatch.setattr(
            umbrella_mod,
            "_resolve_skill_writer",
            lambda name: type("W", (), {"umbrella_skill_char_target": lambda self: 8000})(),
        )
        import models

        monkeypatch.setattr(models, "build_main_llm", lambda **kwargs: fake)

        umbrella_mod._generate_umbrella_skill("async-auth", _REASONS, "SOURCE")

        assert "when_to_use" in fake.system
        assert "REQUIRED" in fake.system
        assert "when_not_to_use: optional" in fake.system
        assert "**When to use:**" in fake.system
        assert "**When NOT to use:**" in fake.system
