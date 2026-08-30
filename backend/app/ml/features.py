"""
The feature contract for the recovery-propensity model.

Why this module exists
----------------------
Training code and serving code must build a feature row *the same way*. The
classic way a working model silently rots is training/serving skew: the training
script derives ``amount_rupees`` one way, the API derives it another, and the
model scores nonsense in production while every unit test still passes.

So this codebase has exactly one function that turns raw payment facts into a
model row -- :func:`build_feature_row` -- and both the synthetic data generator
(``app/ml/dataset.py``) and the live predictor (``app/ml/predictor.py``) call it.
If a feature definition changes, it changes in one place and both sides move
together. That is the entire justification for this file existing separately
from the training script.

Column *order* is part of the contract too. ``ColumnTransformer`` selects by
name, but the one-row DataFrame the predictor builds is constructed with
``columns=FEATURE_COLUMNS``, so a fitted artefact can never be handed columns in
an order it was not fitted on.

This module has no third-party imports on purpose: it can be imported (and
tested) without pandas, numpy or scikit-learn being installed.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, TypeVar

from app.domain.enums import FailureCategory, PaymentMethod, RecoveryStrategy

# ---------------------------------------------------------------------------
# The columns
# ---------------------------------------------------------------------------

#: One-hot encoded. These are the three "what happened / what do we plan to do"
#: facts. They are categorical rather than ordinal because there is no natural
#: ordering: ``upi_timeout`` is not "greater than" ``bank_decline``, and encoding
#: them as integers would invite the model to interpolate between two unrelated
#: failure modes.
CATEGORICAL_FEATURES: list[str] = [
    "failure_category",
    "payment_method",
    "proposed_strategy",
]

#: Standard-scaled. Scaling is not strictly required by a tree ensemble, but the
#: same ``ColumnTransformer`` also feeds the decision-tree baseline and would
#: feed a linear model if one were ever added, and an unscaled ``amount_rupees``
#: (range 100..50,000) next to ``customer_prior_success_rate`` (range 0..1) is a
#: trap waiting for whoever adds that model.
NUMERIC_FEATURES: list[str] = [
    "amount_rupees",
    "attempt_number",
    "customer_prior_success_rate",
    "customer_total_payments",
    "hours_since_failure",
    "is_same_method_retry",
]

#: The exact column order the pipeline is fitted on and scored with.
FEATURE_COLUMNS: list[str] = CATEGORICAL_FEATURES + NUMERIC_FEATURES

#: Binary label: 1 when the recovery attempt collected the money.
TARGET: str = "recovered"


# ---------------------------------------------------------------------------
# Strategy -> rail mapping
# ---------------------------------------------------------------------------

#: Which payment rail each strategy would put the retry on.
#:
#: ``None`` means "the same rail the original payment used". Strategies that are
#: deliberately *absent* from this map -- ``MANUAL_REVIEW`` and ``NO_RECOVERY``
#: -- create no payment at all, which is a third case and must not be confused
#: with ``None``. Using a dict with genuine absence rather than a second
#: sentinel value keeps that three-way distinction honest.
STRATEGY_TARGET_METHOD: dict[RecoveryStrategy, PaymentMethod | None] = {
    RecoveryStrategy.RETRY_SAME_METHOD: None,
    # Retrying later is still the same rail; only the clock changes. It earns a
    # 1 for is_same_method_retry for exactly that reason.
    RecoveryStrategy.RETRY_LATER: None,
    RecoveryStrategy.SWITCH_TO_UPI: PaymentMethod.UPI,
    RecoveryStrategy.SWITCH_TO_CARD: PaymentMethod.CARD,
    RecoveryStrategy.SWITCH_TO_NETBANKING: PaymentMethod.NETBANKING,
}


EnumT = TypeVar("EnumT", bound=Enum)


def coerce_enum_value(value: Any, enum_cls: type[EnumT], default: EnumT) -> str:
    """
    Normalise an enum member, a wire string or junk into a valid enum *value*.

    The categorical columns must contain the same string tokens at inference
    time that they contained at fit time, or ``handle_unknown="ignore"`` will
    quietly encode the row as an all-zero block and the feature will vanish
    without any error being raised. That silent failure is the thing this
    function exists to prevent.

    Args:
        value: An ``enum_cls`` member, a raw string such as ``"bank_decline"``
            (which is what SQLAlchemy hands back, since the columns are plain
            ``String``), or anything else.
        enum_cls: The enum the value is supposed to belong to.
        default: The member to fall back to when ``value`` cannot be resolved.

    Returns:
        The validated enum value as a plain ``str``.
    """
    if isinstance(value, enum_cls):
        return str(value.value)
    try:
        return str(enum_cls(value).value)
    except (ValueError, KeyError, TypeError):
        # Unrecognised input is data corruption, not a programming error, and it
        # must not take down a payment flow. The caller's default carries the
        # domain meaning ("we do not know") so the model still sees a token it
        # was trained on.
        return str(default.value)


def is_same_method_retry(
    payment_method: PaymentMethod | str,
    proposed_strategy: RecoveryStrategy | str,
) -> float:
    """
    1.0 when the proposed strategy re-presents the payment on the same rail.

    Why this is a feature at all: "retry the same method" means something very
    different depending on *why* the payment failed. Re-presenting an expired
    card is hopeless; re-sending a UPI collect request that the customer simply
    missed is close to free money. The model cannot express that interaction
    from ``payment_method`` and ``proposed_strategy`` alone without spending a
    lot of one-hot combinations on it, so the interaction is handed over
    pre-computed.

    Args:
        payment_method: The instrument the original payment used.
        proposed_strategy: The strategy the agent recommended.

    Returns:
        1.0 for a same-rail retry, 0.0 for a rail switch or for a strategy that
        creates no payment at all.
    """
    method = coerce_enum_value(payment_method, PaymentMethod, PaymentMethod.UNKNOWN)
    strategy_value = coerce_enum_value(
        proposed_strategy, RecoveryStrategy, RecoveryStrategy.MANUAL_REVIEW
    )
    strategy = RecoveryStrategy(strategy_value)

    if strategy not in STRATEGY_TARGET_METHOD:
        # MANUAL_REVIEW / NO_RECOVERY: no rail is involved, so "same rail" is
        # false rather than unknown.
        return 0.0

    target = STRATEGY_TARGET_METHOD[strategy]
    if target is None:
        return 1.0
    return 1.0 if target.value == method else 0.0


def build_feature_row(
    *,
    failure_category: FailureCategory | str,
    payment_method: PaymentMethod | str,
    proposed_strategy: RecoveryStrategy | str,
    amount_paise: int,
    attempt_number: int,
    customer_prior_success_rate: float,
    customer_total_payments: int,
    hours_since_failure: float,
) -> dict[str, Any]:
    """
    Build one model row from raw payment facts.

    This is the only place a feature row is constructed anywhere in the project.

    Args:
        failure_category: Classified reason the original payment failed.
        payment_method: Instrument the original payment used.
        proposed_strategy: Recovery strategy under consideration.
        amount_paise: Payment value in **integer paise**, as stored.
        attempt_number: 1 for the first recovery attempt on this case, 2 for the
            second, and so on.
        customer_prior_success_rate: Share of the customer's past payments that
            succeeded. Callers pass ``Customer.prior_success_rate``, which
            already returns the neutral 0.5 for a customer with no history.
        customer_total_payments: How many payments that rate is computed from.
            Carried as its own feature because it says how much the rate can be
            trusted: 1/1 and 40/40 are the same rate and very different evidence.
        hours_since_failure: Age of the original failure, in hours.

    Returns:
        A dict whose keys are exactly ``FEATURE_COLUMNS``.
    """
    method = coerce_enum_value(payment_method, PaymentMethod, PaymentMethod.UNKNOWN)
    strategy = coerce_enum_value(
        proposed_strategy, RecoveryStrategy, RecoveryStrategy.MANUAL_REVIEW
    )
    category = coerce_enum_value(failure_category, FailureCategory, FailureCategory.UNKNOWN)

    return {
        "failure_category": category,
        "payment_method": method,
        "proposed_strategy": strategy,
        # Rupees, not paise, purely as a modelling scale: paise puts the column
        # two orders of magnitude above every other numeric feature and makes the
        # scaler's job (and any future linear model's coefficients) harder to
        # read. This is not money arithmetic -- no value derived here is ever
        # written back to a payment, an order or a ledger entry. The authoritative
        # amount stays integer paise everywhere it matters.
        "amount_rupees": amount_paise / 100.0,
        "attempt_number": float(attempt_number),
        "customer_prior_success_rate": float(customer_prior_success_rate),
        "customer_total_payments": float(customer_total_payments),
        "hours_since_failure": float(hours_since_failure),
        "is_same_method_retry": is_same_method_retry(method, strategy),
    }
