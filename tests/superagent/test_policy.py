"""Policy gate, admission and revalidation tests."""

from __future__ import annotations

from dataclasses import replace

from superagent.admission import AdmissionCell, CellValue, admission_table, cell_of, strength_of
from superagent.core.model import Actor, AddressKind, Mandate, MandateBasis, Strength
from superagent.policy import settings_policy
from tests.superagent.conftest import make_ticket, make_trigger, policy_settings


def test_policy_version_tracks_relevant_settings() -> None:
    first = settings_policy(policy_settings(), "github")
    assert settings_policy(policy_settings(), "github").version == first.version
    changed = policy_settings(accept_mode="auto")
    assert settings_policy(changed, "github").version != first.version


def test_gate_explicit_paths() -> None:
    policy = settings_policy(policy_settings(), "github")
    addressed = replace(make_trigger(), addressed=AddressKind.MENTION)
    result = policy.gate(addressed, None, [])
    assert result.strength == Strength.EXPLICIT
    assert result.basis == MandateBasis.ADDRESSED

    from superagent.core.model import Follow

    ticket = make_ticket()
    ticket.follows = [Follow(thread=addressed.threads[0])]
    followup = replace(make_trigger("trig-follow"), addressed=None)
    assert policy.gate(followup, None, [ticket]).strength == Strength.EXPLICIT


def test_gate_inferred_and_not_admitted() -> None:
    observe = settings_policy(policy_settings(), "github")
    unlisted = replace(make_trigger(), actor=Actor(source="github", id="bob"))
    unlisted = replace(unlisted, addressed=AddressKind.MENTION)
    assert observe.gate(unlisted, None, []).strength is None

    settings = policy_settings()
    settings["sources"]["github"]["admission"]["github/issue"]["unlisted_addressed"] = "inferred"
    inferred = settings_policy(settings, "github").gate(unlisted, None, [])
    assert inferred.strength == Strength.INFERRED

    off = settings_policy(policy_settings(accept_mode="off"), "github")
    assert off.gate(unlisted, None, []).strength is None

    bot = replace(make_trigger("trig-bot"), actor=Actor(source="github", id="dependabot[bot]"))
    assert observe.gate(bot, None, []).strength == Strength.INFERRED


def test_admission_cells_all_values() -> None:
    table = admission_table(
        {
            "admission": {
                "github/issue": {
                    "listed_unaddressed": "explicit",
                    "unlisted_addressed": "inferred",
                    "unlisted_unaddressed": "ignore",
                }
            }
        }
    )
    row = cell_of(table, "github/issue")
    assert row == AdmissionCell(CellValue.EXPLICIT, CellValue.INFERRED, CellValue.IGNORE)
    assert strength_of(CellValue.EXPLICIT) == Strength.EXPLICIT
    assert strength_of(CellValue.INFERRED) == Strength.INFERRED
    assert strength_of(CellValue.IGNORE) is None


def test_revalidation_bases() -> None:
    auto = settings_policy(policy_settings(accept_mode="auto"), "github")
    observe = settings_policy(policy_settings(), "github")

    def mandate(strength: Strength, via: MandateBasis, rules: list[str] | None = None) -> Mandate:
        return Mandate(
            policy_version="v1",
            scope="github",
            strength=strength,
            matched_rules=rules or ["rule-1"],
            sender=Actor(source="github", id="alice"),
            via=via,
            basis_detail={"scope": "github/issue", "cell": strength.value},
        )

    assert auto.revalidate(mandate(Strength.EXPLICIT, MandateBasis.FOLLOWUP)).ok
    assert observe.revalidate(mandate(Strength.INFERRED, MandateBasis.ADDRESSED)).ok is False
    assert auto.revalidate(mandate(Strength.INFERRED, MandateBasis.ADDRESSED)).ok
    assert auto.revalidate(mandate(Strength.INFERRED, MandateBasis.BOT, ["missing"])).ok is False
    assert observe.revalidate(mandate(Strength.INFERRED, MandateBasis.ADMISSION)).ok is False
    assert observe.revalidate(mandate(Strength.EXPLICIT, MandateBasis.ADDRESSED)).ok


def test_admits_inferred_modes() -> None:
    off = settings_policy(policy_settings(accept_mode="off"), "github")
    observe = settings_policy(policy_settings(accept_mode="observe"), "github")
    auto = settings_policy(policy_settings(accept_mode="auto"), "github")
    assert off.admits_inferred() is False
    assert observe.admits_inferred() is True
    assert auto.admits_inferred(["rule-1"]) is True
    assert auto.admits_inferred(["missing"]) is False
