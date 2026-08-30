"""
Failure classification and the recovery playbook.

The product thesis is that the right recovery action differs by *why* the payment
failed. Everything downstream -- the strategy, the propensity score, half the
guardrails -- is conditioned on the category this module assigns. So the two
things worth testing are coverage (no category can be produced that the playbook
has no answer for) and honesty (an unrecognised failure is admitted as unknown
rather than guessed into a plausible-looking category).
"""

from __future__ import annotations

import pytest

from app.agent.taxonomy import (
    DESCRIPTION_PATTERNS,
    ERROR_CODE_MAP,
    PLAYBOOK,
    REASON_MAP,
    classify_error,
)
from app.db.scenarios import SCENARIOS
from app.domain.enums import FailureCategory, RecoveryStrategy
from tests.conftest import field_of


def _scenario_params() -> list:
    """
    Turn the seed catalogue into parametrised cases.

    Reading through ``field_of`` keeps this working whether the catalogue is a
    mapping keyed by scenario name or a plain sequence of records -- the table is
    demo data and gets restructured, and a test that dictates the container shape
    of a fixture file is a test that gets deleted.
    """
    entries = list(SCENARIOS.values()) if hasattr(SCENARIOS, "values") else list(SCENARIOS)
    params = []
    for index, entry in enumerate(entries):
        name = field_of(entry, "name") or field_of(entry, "key") or f"scenario_{index}"
        params.append(pytest.param(entry, id=str(name)))
    return params


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", list(FailureCategory), ids=lambda c: c.value)
def test_every_failure_category_has_a_playbook_entry(category: FailureCategory) -> None:
    """
    The silent-gap bug: add a category to the enum, forget the playbook entry, and
    the first payment that classifies to it takes down the analysis with a
    ``KeyError`` -- in production, on the money path, for the one failure type
    nobody had thought about.

    Parametrised per category rather than asserted as a set difference so that the
    failure message names the missing member instead of printing two sets.
    """
    playbook = PLAYBOOK.get(category)
    assert playbook is not None, f"{category.value} has no playbook entry"
    # A copy-pasted dict literal that forgot to change the category is the other
    # half of this bug, and it produces advice for the wrong failure.
    assert playbook.category is category
    assert playbook.reasoning.strip()
    assert playbook.customer_message.strip()
    assert 0.0 <= playbook.typical_success_rate <= 1.0


def test_a_non_recoverable_category_is_never_given_a_money_moving_playbook() -> None:
    """
    Coverage alone is not safety: an entry could exist and still recommend a
    retry. ``is_recoverable`` and the playbook have to agree, because the playbook
    is what the deterministic planner reads and the guardrail is only the second
    line of defence.
    """
    for category in FailureCategory:
        if category.is_recoverable:
            continue
        assert PLAYBOOK[category].primary_strategy.moves_money is False
        assert PLAYBOOK[category].alternate_strategy.moves_money is False


def test_risk_blocked_is_excluded_from_recovery_entirely() -> None:
    """
    Named explicitly because this is the one category where being wrong helps
    someone push a stolen instrument through, and the cost of the mistake lands on
    the cardholder rather than on the merchant.
    """
    assert FailureCategory.RISK_BLOCKED.is_recoverable is False
    assert PLAYBOOK[FailureCategory.RISK_BLOCKED].primary_strategy is RecoveryStrategy.NO_RECOVERY


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_the_seed_catalogue_is_not_empty() -> None:
    """
    Guards the parametrised test below: an empty catalogue would silently collect
    zero cases and report green.
    """
    assert _scenario_params()


@pytest.mark.parametrize("scenario", _scenario_params())
def test_each_seeded_scenario_classifies_to_the_category_it_advertises(scenario) -> None:
    """
    The demo data and the classifier must not drift apart.

    Every scenario in the catalogue declares the category it is meant to
    demonstrate. If a Razorpay error code is renamed in the taxonomy, or a
    scenario's error fields are edited, this is what catches the demo quietly
    showing "unknown -> manual review" for a failure the script says is a UPI
    timeout.
    """
    expected = field_of(scenario, "expected_category")
    assert expected is not None, "scenario declares no expected_category"

    match = classify_error(
        error_code=field_of(scenario, "error_code"),
        error_reason=field_of(scenario, "error_reason"),
        error_description=field_of(scenario, "error_description"),
        error_source=field_of(scenario, "error_source"),
        error_step=field_of(scenario, "error_step"),
        method=field_of(scenario, "method"),
    )
    assert match.category is FailureCategory(expected)
    assert match.evidence, "a classification with no evidence cannot be reviewed"


def test_an_unrecognisable_failure_is_admitted_rather_than_guessed() -> None:
    """
    Absence of evidence is not evidence of safety.

    ``UNKNOWN`` is deliberately non-recoverable, so guessing a plausible category
    for an unmapped gateway error is not a cosmetic mistake -- it converts a
    failure nobody understood into an automated re-charge. The confidence has to
    be low as well as the category correct, because the score is what the operator
    reads on the approval screen.
    """
    match = classify_error(
        error_code="QZX_9182_NOT_A_REAL_CODE",
        error_reason="qzx_9182",
        error_description="lorem ipsum dolor sit amet",
    )
    assert match.category is FailureCategory.UNKNOWN
    assert match.matched_on == "default"
    assert match.confidence < 0.5


def test_classification_ignores_case_and_surrounding_whitespace() -> None:
    """
    Gateway payloads arrive as free-form JSON from several sources -- a webhook, a
    polled fetch, a hand-written test fixture -- and casing is not guaranteed. A
    classifier that only matched the exact stored spelling would degrade to
    "unknown" on real traffic while passing every test written from the map's own
    keys.
    """
    assert ERROR_CODE_MAP, "the error-code table is the primary classification signal"
    code, expected = next(iter(ERROR_CODE_MAP.items()))
    assert classify_error(error_code=f"  {code.lower()}  ").category is expected

    assert REASON_MAP, "the reason table is the secondary classification signal"
    reason, expected_reason = next(iter(REASON_MAP.items()))
    assert classify_error(error_reason=f" {reason.upper()} ").category is expected_reason


def test_a_free_text_description_can_classify_when_no_code_is_present() -> None:
    """
    Not every failure arrives with a machine code -- older webhooks and some
    netbanking rails send prose only. The description patterns are the last
    structured signal before ``UNKNOWN``, so they have to work on their own.
    """
    assert DESCRIPTION_PATTERNS, "no description fallback means prose-only failures are all unknown"
    # The first pattern specifically: patterns are matched in declaration order,
    # so wrapping prose cannot accidentally satisfy an earlier entry and make this
    # test pass while claiming to exercise a different one.
    pattern, expected = DESCRIPTION_PATTERNS[0]
    match = classify_error(error_description=f"  {pattern.upper()}  ")
    assert match.category is expected
