"""
Serving front door for the recovery-propensity model.

Two things this module guarantees, and everything in it exists for one of them:

1.  **``predict()`` never raises.** It sits inside the payment analysis flow. A
    missing model file, a pickle written by a different scikit-learn version, a
    corrupt artefact, a feature dict with a key spelled wrong -- none of these
    may turn into a 500 on a merchant's payment screen. Every one of them
    degrades to a documented heuristic and says so via ``is_fallback``.

2.  **A degraded score is labelled, never disguised.** ``PropensityResultOut``
    carries ``is_fallback`` and ``model_version`` all the way to the UI, so an
    operator approving a recovery can see whether the number came from a trained
    model or from an if/else. A demo that quietly falls back and shows the same
    confident number either way is lying to its user.

The recall trade-off, restated where it is enforced
---------------------------------------------------
``app/ml/train.py`` explains at length why this model is tuned towards recall:
a false positive costs one gateway call, a false negative costs the merchant the
whole transaction. The consequence lands here. The score this module returns is
compared against ``settings.min_propensity_score`` (0.15) by guardrail
``R10_PROPENSITY_FLOOR``. That floor is deliberately low -- it screens out the
genuinely hopeless (risk-blocked transactions, dead cards being re-presented,
third attempts on a week-old failure) and passes everything else to a human.
The model's job is not to decide; it is to avoid wasting a person's attention on
cases that cannot work.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from app.config import MODELS_DIR
from app.domain.enums import FailureCategory, PaymentMethod, RecoveryStrategy
from app.domain.schemas import PropensityResultOut
from app.ml.dataset import (
    CATEGORY_BASE_RATE,
    attempt_factor,
    customer_quality_factor,
    strategy_fit,
)
from app.ml.features import (
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    NUMERIC_FEATURES,
    coerce_enum_value,
)

logger = logging.getLogger(__name__)

#: Written by ``app/ml/train.py``. Duplicated here rather than imported from the
#: training module so that serving never pulls scikit-learn's model-selection
#: stack into the API process just to read a filename.
MODEL_FILENAME = "propensity_model.joblib"
METRICS_FILENAME = "metrics.json"

#: Reported as ``model_version`` whenever the heuristic produced the score. It is
#: deliberately not shaped like a trained version string ("v1-..."), so nobody
#: skim-reading a case row mistakes one for the other.
HEURISTIC_MODEL_VERSION = "heuristic-v1"

#: Used when the artefact loaded but could not name itself.
UNKNOWN_MODEL_VERSION = "v1-unknown"

#: Fallback base rate for a category with no tabulated rate, matching
#: ``FailureCategory.UNKNOWN``.
DEFAULT_BASE_RATE = 0.25

#: The heuristic is clamped rather than allowed to reach 0.0 or 1.0. A rule of
#: thumb that claims certainty in either direction is overselling itself.
HEURISTIC_FLOOR = 0.02
HEURISTIC_CEILING = 0.95

#: Defaults applied to a feature dict that is missing keys. ``predict()`` is
#: contractually total, so the coercion happens here, once, at the boundary that
#: owns the model's input contract -- and nowhere else in the call chain.
_CATEGORICAL_DEFAULTS: dict[str, Any] = {
    "failure_category": FailureCategory.UNKNOWN,
    "payment_method": PaymentMethod.UNKNOWN,
    "proposed_strategy": RecoveryStrategy.MANUAL_REVIEW,
}
_CATEGORICAL_ENUMS: dict[str, Any] = {
    "failure_category": FailureCategory,
    "payment_method": PaymentMethod,
    "proposed_strategy": RecoveryStrategy,
}
_NUMERIC_DEFAULTS: dict[str, float] = {
    "amount_rupees": 0.0,
    "attempt_number": 1.0,
    # The same neutral prior the ORM uses for a customer with no history.
    "customer_prior_success_rate": 0.5,
    "customer_total_payments": 0.0,
    "hours_since_failure": 0.0,
    "is_same_method_retry": 0.0,
}

#: Short, operator-facing names for each failure category. A derived label
#: (``"gateway_error".replace("_", " ")``) would never drift, but it reads like a
#: log line rather than an explanation, and this string appears on the screen
#: where a human decides whether to move money. The ``.get()`` below falls back
#: to the derived form, so a category added later degrades to a slightly clumsy
#: label instead of raising.
_CATEGORY_LABEL: dict[FailureCategory, str] = {
    FailureCategory.BANK_DECLINE: "Issuer declined the payment",
    FailureCategory.INSUFFICIENT_FUNDS: "Insufficient funds",
    FailureCategory.UPI_TIMEOUT: "UPI collect request expired",
    FailureCategory.SESSION_EXPIRED: "Checkout session expired",
    FailureCategory.GATEWAY_ERROR: "Gateway-side failure",
    FailureCategory.NETWORK_ERROR: "Network-level failure",
    FailureCategory.AUTHENTICATION_FAILED: "Authentication not completed",
    FailureCategory.INVALID_INSTRUMENT: "Card details invalid or expired",
    FailureCategory.RISK_BLOCKED: "Blocked by risk checks",
    FailureCategory.CUSTOMER_ABANDONED: "Customer abandoned checkout",
    FailureCategory.UNKNOWN: "Failure reason could not be classified",
}

#: Cap on how many drivers are surfaced. Four is what fits in the approval
#: screen's sidebar without the operator having to scroll during an approval
#: decision, and a ranked list nobody reads to the end is not explainability.
MAX_TOP_FACTORS = 4


def _clamp(value: float, low: float, high: float) -> float:
    """Constrain ``value`` to ``[low, high]``."""
    return min(high, max(low, value))


def _as_float(value: Any, default: float) -> float:
    """
    Coerce to a finite float, falling back to ``default`` for None or junk.

    Non-finite values are rejected as firmly as unparseable ones. NaN would slip
    through the scaler and come back out of ``predict_proba`` as a NaN score that
    then fails Pydantic's ``ge=0.0`` bound; infinity survives arithmetic quite
    happily right up until ``int(inf)`` raises ``OverflowError`` while building
    an explanation string. Both are caught in one place, here, because this is
    the only door numeric input comes through.
    """
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _coerce_row(features: Mapping[str, Any]) -> dict[str, Any]:
    """
    Normalise an arbitrary feature mapping into a valid model row.

    Args:
        features: Ideally the output of
            :func:`app.ml.features.build_feature_row`, but tolerated in any
            shape, because ``predict()`` may not raise.

    Returns:
        A dict with exactly ``FEATURE_COLUMNS`` keys, categorical values
        guaranteed to be valid enum values and numeric values guaranteed finite
        and in range.
    """
    row: dict[str, Any] = {}

    for column in CATEGORICAL_FEATURES:
        row[column] = coerce_enum_value(
            features.get(column),
            _CATEGORICAL_ENUMS[column],
            _CATEGORICAL_DEFAULTS[column],
        )

    for column in NUMERIC_FEATURES:
        row[column] = _as_float(features.get(column), _NUMERIC_DEFAULTS[column])

    # Range constraints, expressed once. These are not re-validations of a
    # guardrail: they exist because a scaler fitted on hours in [0, 168] has no
    # sensible response to -4, and a negative attempt number is not a policy
    # violation, it is nonsense.
    row["amount_rupees"] = max(0.0, row["amount_rupees"])
    row["attempt_number"] = max(1.0, row["attempt_number"])
    row["customer_prior_success_rate"] = _clamp(row["customer_prior_success_rate"], 0.0, 1.0)
    row["customer_total_payments"] = max(0.0, row["customer_total_payments"])
    row["hours_since_failure"] = max(0.0, row["hours_since_failure"])
    row["is_same_method_retry"] = 1.0 if row["is_same_method_retry"] >= 0.5 else 0.0

    return row


def _top_factors(row: Mapping[str, Any]) -> list[str]:
    """
    Turn a feature row into short, human-readable drivers of the score.

    These are explanations, not attributions: they name the facts a payments
    person would cite, in the order that person would cite them, rather than
    reporting SHAP values an operator cannot act on. The model's global feature
    importances live in ``metrics.json`` for anyone who wants the statistical
    view.

    Args:
        row: A coerced feature row.

    Returns:
        At most ``MAX_TOP_FACTORS`` strings, most important first.
    """
    category = FailureCategory(row["failure_category"])
    strategy = RecoveryStrategy(row["proposed_strategy"])

    base_rate = CATEGORY_BASE_RATE.get(category, DEFAULT_BASE_RATE)
    label = _CATEGORY_LABEL.get(category, category.value.replace("_", " ").capitalize())

    factors: list[str] = [
        f"{label} - historically {round(base_rate * 100)}% recoverable"
    ]

    # Strategy fit second: it is the only factor on this list the operator can
    # still change before approving.
    fit = strategy_fit(category, strategy)
    strategy_label = strategy.value.replace("_", " ")
    if fit >= 1.10:
        factors.append(f"'{strategy_label}' is a strong fit for this failure")
    elif fit <= 0.55:
        factors.append(f"'{strategy_label}' is a poor fit for this failure")

    attempts = int(row["attempt_number"])
    if attempts == 2:
        factors.append("Second attempt reduces likelihood")
    elif attempts >= 3:
        factors.append(f"Attempt {attempts} - earlier tries already failed")

    total_payments = int(row["customer_total_payments"])
    success_pct = round(row["customer_prior_success_rate"] * 100)
    if total_payments >= 3:
        # Phrased with the percentage at the end rather than as "has an 88%
        # history": the correct article depends on how the number is *spoken*
        # ("an 88%", "a 40%"), and a string that reads wrong on some values is a
        # string that reads wrong in front of a customer.
        factors.append(
            f"Customer has a payment success history of {success_pct}% "
            f"over {total_payments} payments"
        )
    elif total_payments == 0:
        factors.append("New customer - neutral 50% prior applied")
    else:
        factors.append(f"Only {total_payments} prior payment(s) - thin history")

    hours = row["hours_since_failure"]
    if hours >= 48:
        factors.append(f"Failed {int(hours // 24)} days ago - customer intent has decayed")

    amount_rupees = row["amount_rupees"]
    if amount_rupees >= 10_000:
        factors.append(f"High value (Rs {amount_rupees:,.0f}) slightly lowers the odds")

    return factors[:MAX_TOP_FACTORS]


class PropensityPredictor:
    """
    Loads the trained pipeline once and scores one payment at a time.

    Attributes:
        is_loaded: True when the trained artefact was found and unpickled.
        model_version: The artefact's version string, or
            ``HEURISTIC_MODEL_VERSION`` when no artefact is in use.
    """

    def __init__(self, models_dir: Path | None = None) -> None:
        """
        Args:
            models_dir: Directory holding the artefact. Defaults to
                ``config.MODELS_DIR``; overridable so tests can point at a
                temporary directory without touching the real one.
        """
        self._models_dir = models_dir or MODELS_DIR
        self._pipeline: Any = None
        self._load_attempted = False
        self._is_loaded = False
        self._model_version = HEURISTIC_MODEL_VERSION

    # -- loading ----------------------------------------------------------

    def _load(self) -> None:
        """
        Load the artefact on first use.

        Deferred rather than done in ``__init__`` for two reasons: importing
        joblib pulls in scikit-learn, which costs a second or two of process
        start-up that an app running purely on the heuristic should not pay; and
        a training run that finishes after the API started is then picked up by
        the next process without any special handling.

        A concurrent double-load is possible (FastAPI runs sync endpoints in a
        threadpool) and is deliberately not locked against: both threads produce
        an equivalent pipeline and the last assignment wins, so the only cost is
        a few wasted milliseconds on one request, which is cheaper than the lock
        it would take to prevent it.
        """
        if self._load_attempted:
            return
        self._load_attempted = True

        model_path = self._models_dir / MODEL_FILENAME
        if not model_path.exists():
            logger.info(
                "Propensity model not found at %s; using the heuristic fallback. "
                "Run `python -m app.ml.train` to train it.",
                model_path,
            )
            return

        try:
            import joblib  # Imported here so the heuristic path never needs it.

            pipeline = joblib.load(model_path)
        except Exception:
            # Broad on purpose. Unpickling an estimator can fail in ways that
            # have no shared base class: a truncated file, a numpy ABI mismatch,
            # a pickle written by a different scikit-learn version, a module that
            # no longer exists. Enumerating them would be guesswork, and the
            # correct response is identical for all of them -- score with the
            # heuristic and keep the payments API up. A model artefact is not
            # allowed to take down a payment flow.
            logger.warning(
                "Failed to load the propensity model from %s; falling back to the "
                "heuristic. The model file is likely corrupt or was written by a "
                "different scikit-learn version.",
                model_path,
                exc_info=True,
            )
            return

        self._pipeline = pipeline
        self._is_loaded = True
        self._model_version = self._resolve_version(pipeline)
        logger.info("Loaded propensity model %s from %s", self._model_version, model_path)

    def _resolve_version(self, pipeline: Any) -> str:
        """
        Determine which model is in memory.

        Prefers the version stamped onto the artefact at training time, because
        that value cannot be separated from the model it describes. Falls back to
        ``metrics.json``, which is right next to the artefact but is a separate
        file and could have come from a different run.

        Args:
            pipeline: The unpickled pipeline.

        Returns:
            A version string; ``UNKNOWN_MODEL_VERSION`` if neither source
            answers.
        """
        stamped = getattr(pipeline, "recoverai_model_version", None)
        if isinstance(stamped, str) and stamped:
            return stamped

        metrics_path = self._models_dir / METRICS_FILENAME
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            version = metrics.get("model_version")
            if isinstance(version, str) and version:
                return version
        except Exception:
            # Same reasoning as the load above: not knowing the version is a
            # labelling problem, not a reason to fail a prediction.
            logger.debug("Could not read %s for a model version", metrics_path, exc_info=True)

        return UNKNOWN_MODEL_VERSION

    @property
    def is_loaded(self) -> bool:
        """True when a trained artefact is in memory."""
        self._load()
        return self._is_loaded

    @property
    def model_version(self) -> str:
        """Version of whatever produced the scores -- model or heuristic."""
        self._load()
        return self._model_version

    # -- scoring ----------------------------------------------------------

    def predict(self, features: dict[str, Any]) -> PropensityResultOut:
        """
        Score one recovery.

        Never raises. Any failure in the model path is logged and answered with
        the heuristic, flagged as ``is_fallback=True``.

        Args:
            features: A feature mapping, normally built by
                :func:`app.ml.features.build_feature_row`.

        Returns:
            A ``PropensityResultOut`` with the score, the version of whatever
            produced it, the drivers behind it, and whether it is a fallback.
        """
        row = _coerce_row(features)
        self._load()

        if self._pipeline is not None:
            try:
                # Column order is pinned explicitly. The pipeline selects by
                # name, but building the frame from FEATURE_COLUMNS means a
                # caller's dict ordering can never reach the model at all.
                frame = pd.DataFrame([row], columns=FEATURE_COLUMNS)
                score = float(self._pipeline.predict_proba(frame)[0][1])
                return PropensityResultOut(
                    score=_clamp(score, 0.0, 1.0),
                    model_version=self._model_version,
                    top_factors=_top_factors(row),
                    is_fallback=False,
                )
            except Exception:
                # Broad for the same reason as the load: a scoring failure -- an
                # unexpected dtype, a transformer raising on an edge case -- must
                # cost accuracy, not availability.
                logger.warning(
                    "Propensity model scoring failed; falling back to the heuristic.",
                    exc_info=True,
                )

        return self._heuristic(row)

    def _heuristic(self, row: Mapping[str, Any]) -> PropensityResultOut:
        """
        The documented rule-of-thumb score used when no model is available.

        This is what makes the whole application runnable before anybody has run
        training: clone, install, start, and every screen works -- the propensity
        column is populated, guardrail R10 has a number to compare against, and
        the UI shows a "heuristic" badge instead of a blank.

        It reuses ``CATEGORY_BASE_RATE`` and the attempt and customer multipliers
        straight from ``app/ml/dataset.py``, which are the same assumptions the
        model was trained on. That is the point: the fallback is a coarse version
        of the model rather than a second, contradictory opinion.

        It deliberately stops at two adjustments. Strategy fit, amount and
        recency all belong in the real model; folding them in here would make the
        heuristic look like a model and invite people to trust it as one. A
        fallback should be visibly cruder than the thing it replaces.

        Args:
            row: A coerced feature row.

        Returns:
            A ``PropensityResultOut`` with ``is_fallback=True``.
        """
        category = FailureCategory(row["failure_category"])
        base_rate = CATEGORY_BASE_RATE.get(category, DEFAULT_BASE_RATE)

        score = (
            base_rate
            * attempt_factor(int(row["attempt_number"]))
            * customer_quality_factor(row["customer_prior_success_rate"])
        )

        return PropensityResultOut(
            score=_clamp(score, HEURISTIC_FLOOR, HEURISTIC_CEILING),
            model_version=HEURISTIC_MODEL_VERSION,
            top_factors=_top_factors(row),
            is_fallback=True,
        )


@lru_cache(maxsize=1)
def get_predictor() -> PropensityPredictor:
    """
    Process-wide predictor singleton.

    Cached so the artefact is unpickled once rather than once per request -- a
    gradient-boosted ensemble takes long enough to load that doing it inside a
    request handler would be visible in the API's latency.
    """
    return PropensityPredictor()


def reset_predictor() -> None:
    """
    Drop the cached predictor.

    For tests, and for the case where a training run finishes while the process
    is alive: the next ``get_predictor()`` builds a fresh instance that will look
    for the artefact again.
    """
    get_predictor.cache_clear()
