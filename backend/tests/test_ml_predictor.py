"""
The recovery-propensity model at inference time.

The score this module returns feeds a guardrail (``R10_PROPENSITY_FLOOR``) and is
shown to the operator on the approval screen, which makes its *availability* a
safety property rather than a nicety: if prediction can raise, a missing or
half-written model file takes the whole recovery flow down, and if it can return
nonsense the floor rule silently stops meaning anything.

So these tests are about degradation, range and ordering -- not about accuracy.
Accuracy belongs to the training run and its metrics file; a unit test that
asserted a particular score would be pinning one artefact's arithmetic and would
have to be rewritten every time the model was retrained.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.enums import FailureCategory, PaymentMethod, RecoveryStrategy
from app.ml import predictor as predictor_module
from app.ml.features import build_feature_row
from app.ml.predictor import MODEL_FILENAME, get_predictor, reset_predictor


def features(
    *,
    failure_category: FailureCategory = FailureCategory.GATEWAY_ERROR,
    proposed_strategy: RecoveryStrategy = RecoveryStrategy.RETRY_SAME_METHOD,
    payment_method: PaymentMethod = PaymentMethod.CARD,
    amount_paise: int = 250_000,
    attempt_number: int = 1,
    customer_prior_success_rate: float = 0.80,
    customer_total_payments: int = 10,
    hours_since_failure: float = 1.0,
) -> dict:
    """One feature row, built through the production builder rather than by hand."""
    return build_feature_row(
        failure_category=failure_category,
        payment_method=payment_method,
        proposed_strategy=proposed_strategy,
        amount_paise=amount_paise,
        attempt_number=attempt_number,
        customer_prior_success_rate=customer_prior_success_rate,
        customer_total_payments=customer_total_payments,
        hours_since_failure=hours_since_failure,
    )


@pytest.fixture(autouse=True)
def _isolated_predictor():
    """
    Drop the cached singleton around every test in this module.

    ``get_predictor`` is an ``lru_cache``, so a test that pointed the predictor at
    an empty directory would otherwise hand its fallback instance to the next
    test, and the ordering assertions would silently be checking the heuristic
    when they meant to check the trained model.
    """
    reset_predictor()
    yield
    reset_predictor()


@pytest.fixture()
def predictor_looking_at(monkeypatch: pytest.MonkeyPatch):
    """
    Build a predictor that resolves its artefact inside ``directory``.

    Redirecting the module's directory constant, rather than passing a path to the
    constructor, keeps the test independent of how the predictor happens to be
    constructed -- it is always reached through ``get_predictor()``, which is the
    only entry point the application uses. ``MODEL_PATH`` is patched too, harmlessly,
    in case the artefact location is resolved once at import instead of per call.
    """

    def _at(directory: Path):
        monkeypatch.setattr(predictor_module, "MODELS_DIR", directory, raising=False)
        monkeypatch.setattr(
            predictor_module, "MODEL_PATH", directory / MODEL_FILENAME, raising=False
        )
        reset_predictor()
        return get_predictor()

    return _at


# ---------------------------------------------------------------------------
# Range
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", list(FailureCategory), ids=lambda c: c.value)
def test_every_failure_category_yields_a_usable_probability(
    category: FailureCategory,
) -> None:
    """
    A category the encoder has never seen must not produce a NaN, a negative
    number or an exception.

    This is the realistic drift: a category is added to the enum, the model is not
    retrained, and the unseen label reaches a one-hot encoder in production. The
    guardrail compares the score against a floor, so a NaN would compare false
    against every threshold and quietly wave the recovery through.
    """
    result = get_predictor().predict(features(failure_category=category))
    assert 0.0 <= result.score <= 1.0
    assert result.score == result.score  # NaN is the only value that fails this
    assert result.model_version


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def test_a_missing_artefact_falls_back_and_says_so(predictor_looking_at, tmp_path: Path) -> None:
    """
    The zero-setup path: a reviewer clones the project and runs it without ever
    training a model. That must produce a working, honestly-labelled score, since
    an unlabelled heuristic dressed up as a model prediction is the kind of thing
    that makes a whole demo untrustworthy.
    """
    predictor = predictor_looking_at(tmp_path)  # empty directory
    result = predictor.predict(features())

    assert predictor.is_loaded is False
    assert result.is_fallback is True
    assert 0.0 <= result.score <= 1.0
    assert result.top_factors, "a fallback score still has to explain itself"


def test_a_corrupt_artefact_degrades_instead_of_raising(
    predictor_looking_at, tmp_path: Path
) -> None:
    """
    An interrupted training run, a half-copied file or a joblib written by an
    incompatible scikit-learn version all present as a file that exists and will
    not load.

    That is strictly more dangerous than a missing file, because "does the path
    exist?" reports healthy. ``predict()`` is documented never to raise, and this
    is the case that documentation exists for.
    """
    artefact = tmp_path / MODEL_FILENAME
    artefact.write_bytes(b"\x00\x01 this is not a joblib archive \xff\xfe")

    predictor = predictor_looking_at(tmp_path)
    result = predictor.predict(features())

    assert predictor.is_loaded is False
    assert result.is_fallback is True
    assert 0.0 <= result.score <= 1.0


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_a_gateway_fault_scores_higher_than_a_risk_blocked_failure() -> None:
    """
    The one ordering the domain guarantees, and the cheapest possible check that
    the features actually reach the estimator.

    A transient acquirer fault is the textbook recoverable failure; a risk-blocked
    transaction is the textbook unrecoverable one. If a model, an encoder or a
    fallback heuristic ranked them the other way round, it is either ignoring the
    category feature entirely or has its label mapping inverted -- both of which
    produce plausible-looking scores that are worthless to the guardrail.

    Asserted against whichever engine is live: the ordering is a property of the
    problem, so the trained model and the documented heuristic must both honour it.
    """
    predictor = get_predictor()
    recoverable = predictor.predict(features(failure_category=FailureCategory.GATEWAY_ERROR))
    unrecoverable = predictor.predict(
        features(
            failure_category=FailureCategory.RISK_BLOCKED,
            proposed_strategy=RecoveryStrategy.RETRY_SAME_METHOD,
        )
    )
    assert recoverable.score > unrecoverable.score


# ---------------------------------------------------------------------------
# Counterfactual validity
# ---------------------------------------------------------------------------


#: (failure_category, payment_method, better_strategy, worse_strategy)
#:
#: Each row is a claim the product makes out loud. The playbook in
#: ``app.agent.taxonomy`` tells a merchant "for this failure, do X, not Y", and
#: the propensity gauge sits directly beside that recommendation on the approval
#: screen. If the model ranked Y above X, the two halves of the same screen would
#: contradict each other and the operator would be right to trust neither.
PLAYBOOK_ORDERINGS: tuple[tuple[str, str, str, str], ...] = (
    # The headline case. Re-presenting a card the issuer just refused is the
    # single most common wasted retry in payments; UPI takes a different
    # authorisation path, so the issuer's card-side answer does not follow it.
    ("bank_decline", "card", "switch_to_upi", "retry_same_method"),
    # The balance does not change because we asked twice.
    ("insufficient_funds", "upi", "retry_later", "retry_same_method"),
    # The instrument itself is dead, so anything reusing it is wasted.
    ("invalid_instrument", "card", "switch_to_upi", "retry_same_method"),
    # Nothing was declined and nothing is wrong with the instrument -- our side
    # faulted, so the same rail is the right one.
    ("gateway_error", "card", "retry_same_method", "switch_to_upi"),
    # A collect request that expired unanswered is not a refusal.
    ("upi_timeout", "upi", "retry_same_method", "switch_to_card"),
    # OTP friction is card-specific; UPI's own PIN flow is usually easier.
    ("authentication_failed", "card", "switch_to_upi", "retry_same_method"),
)


@pytest.mark.parametrize(
    ("category", "method", "better", "worse"),
    PLAYBOOK_ORDERINGS,
    ids=[f"{c}:{b}>{w}" for c, _, b, w in PLAYBOOK_ORDERINGS],
)
def test_the_model_ranks_the_playbook_strategy_above_the_wrong_one(
    category: str, method: str, better: str, worse: str
) -> None:
    """
    The bug this catches, and it is not hypothetical -- it shipped once.

    ``proposed_strategy`` is not sampled independently of ``failure_category``
    when the training set is generated: the planner mostly proposes the playbook
    answer, so that is mostly what the data contains. With too little exploration
    the counterfactual cells (bank decline *with* a same-method retry) held only a
    few dozen rows, and the model could not separate that interaction from the
    strong global association between "retry the same method" and the easy
    transient categories. It therefore scored re-presenting a declined card
    *above* switching the customer to UPI -- exactly the wasted retry this whole
    system exists to prevent, asserted by the product's own ML.

    The fix was to raise ``PLAYBOOK_SAMPLING_RATE`` exploration, the sample count
    and the tree depth together. This test is what stops any of the three being
    tuned back down without someone noticing, because the symptom is invisible in
    ROC-AUC: the broken model scored 0.78 and looked perfectly healthy.

    Skipped when the artefact is absent, because the heuristic fallback is a
    lookup over base rates and cannot exhibit this failure mode by construction.
    """
    predictor = get_predictor()
    if not predictor.is_loaded:
        pytest.skip("No trained artefact; run `python dev.py train` to exercise this.")

    def score(strategy: str) -> float:
        return predictor.predict(
            build_feature_row(
                failure_category=category,
                payment_method=method,
                proposed_strategy=strategy,
                amount_paise=249_900,
                attempt_number=1,
                customer_prior_success_rate=0.8,
                customer_total_payments=10,
                hours_since_failure=1.0,
            )
        ).score

    better_score, worse_score = score(better), score(worse)
    assert better_score > worse_score, (
        f"For a {category} failure on {method}, the model rates '{worse}' "
        f"({worse_score:.3f}) at or above '{better}' ({better_score:.3f}). "
        "The propensity score now contradicts the recovery playbook shown next to "
        "it on the approval screen."
    )
