"""
Train the recovery-propensity model and write it, plus its metrics, to disk.

Run it with::

    python -m app.ml.train
    python -m app.ml.train --samples 20000 --seed 7

The error the model is tuned to avoid
-------------------------------------
Choosing a classifier means choosing which mistake you would rather make, and
that choice belongs to the domain, not to the library. The two mistakes here are
not symmetric:

*   A **false positive** -- we predict a recovery would succeed, we attempt it,
    it fails. Cost: one gateway API call (effectively free) and a small amount of
    customer friction, bounded by guardrails R1 (at most two attempts), R2 (a
    15-minute cooldown) and R8 (at most three cases per customer per day).

*   A **false negative** -- we predict the recovery is hopeless, so we never
    attempt it, and a payment that would have collected is written off. Cost: the
    **entire transaction value**, permanently, plus the customer relationship
    that came with it.

A false negative is therefore worth many false positives, and the model is tuned
towards **recall** accordingly. The concrete consequences of that choice:

*   ``settings.min_propensity_score`` is set to **0.15**, not to the reflexive
    0.5. It is a floor whose only job is to screen out the genuinely hopeless
    -- risk-blocked transactions, dead instruments being re-presented, third
    attempts on a stale case. Everything with a real pulse gets through to a
    human, who is far better placed than a synthetic-data model to make the
    marginal call.
*   The report below prints recall at the standard 0.5 threshold *and* recall at
    the policy floor the system actually runs on, so the claim "tuned towards
    recall" is backed by the number the guardrail will really use rather than by
    an adjective.

This is the same reasoning shape as a medical screening classifier -- where a
missed diagnosis costs incomparably more than a false alarm and recall is
therefore bought with precision -- but the direction has to be re-derived from
this domain's costs rather than assumed. It happens to land the same way. If the
economics were reversed (say, each attempt cost the merchant a chargeback fee),
the correct floor would be far higher, and the right response would be to change
``min_propensity_score`` in the config, not to retrain anything.

Why gradient boosting
---------------------
The report trains a ``DecisionTreeClassifier(max_depth=5)`` on the identical
preprocessing pipeline and prints both models side by side. A single shallow tree
is the right baseline because it is what a sensible engineer would write by hand
-- essentially a nested if/else over failure category and attempt number -- so
beating it is the minimum bar for the extra dependency and the extra opacity an
ensemble costs. Reporting both means the model choice is evidenced instead of
asserted, and if the baseline ever wins, that is a result worth acting on.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.tree import DecisionTreeClassifier

from app.config import MODELS_DIR, get_settings
from app.db.models import utcnow
from app.domain.enums import FailureCategory, RecoveryStrategy
from app.ml.dataset import DEFAULT_STRATEGY_FIT, STRATEGY_FIT, CATEGORY_BASE_RATE, generate_dataset
from app.ml.features import (
    CATEGORICAL_FEATURES,
    FEATURE_COLUMNS,
    NUMERIC_FEATURES,
    TARGET,
)

#: Artefact names. ``predictor.py`` reads both; they are duplicated there rather
#: than imported from here so that serving never has to import the training
#: module (and with it scikit-learn's model-selection stack).
MODEL_FILENAME = "propensity_model.joblib"
METRICS_FILENAME = "metrics.json"

#: Identifies the algorithm in ``model_version``.
ALGORITHM = "gradient_boosting"
BASELINE_ALGORITHM = "decision_tree"

#: Threshold used for the reported classification metrics. 0.5 is the
#: conventional reporting point and is what makes accuracy/precision/recall
#: comparable to any other binary classifier. It is *not* the threshold the
#: system operates at -- that is ``settings.min_propensity_score`` -- and the
#: report prints both so the difference cannot be mistaken for an oversight.
DECISION_THRESHOLD = 0.5

#: Held out from training entirely and scored exactly once, at the end.
TEST_SIZE = 0.2

#: Number of top features kept in metrics.json. Twelve is enough to cover every
#: numeric feature plus the categories that actually matter, and short enough to
#: read without scrolling.
TOP_FEATURES = 12


def build_pipeline(seed: int) -> Pipeline:
    """
    Assemble the preprocessing + classifier pipeline.

    Everything lives inside one ``Pipeline`` so that the fitted encoder and
    scaler travel with the model in the same ``.joblib`` file. The alternative --
    pickling the estimator alone and re-deriving the encoding at inference --
    is the single most common way a deployed model starts scoring garbage, since
    nothing then forces the serving-side encoding to match the training-side one.

    Args:
        seed: Fixed for reproducibility; passed to the classifier only, since the
            preprocessing steps are deterministic.

    Returns:
        An unfitted ``Pipeline`` with steps ``prep`` and ``clf``.
    """
    preprocessor = ColumnTransformer(
        [
            (
                "cat",
                # handle_unknown="ignore" so that a category the encoder never
                # saw -- a new FailureCategory added after this model was
                # trained -- produces an all-zero block instead of raising in the
                # middle of a payment flow. The cost is that the feature silently
                # goes missing for that row, which is why dataset.py takes care
                # to generate every enum member the system can produce.
                # sparse_output=False keeps the matrix dense: it costs nothing at
                # this width and it makes get_feature_names_out() line up with
                # feature_importances_ without any index gymnastics.
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                CATEGORICAL_FEATURES,
            ),
            ("num", StandardScaler(), NUMERIC_FEATURES),
        ]
    )

    classifier = GradientBoostingClassifier(
        # 250 shallow trees with a low learning rate: many small corrections
        # generalise better on a noisy label than a few aggressive ones, and this
        # label is noisy by construction.
        n_estimators=250,
        learning_rate=0.08,
        # Depth 4, not 3. The interaction this model exists to represent is
        # three-way -- failure category x proposed strategy x whether that
        # strategy reuses the failed instrument -- and depth 3 could not hold it
        # against the much stronger marginal effect of the strategy alone. The
        # symptom was a model that rated re-presenting a declined card above
        # switching rails. Depth was raised together with the sampling
        # exploration rate in dataset.py; neither alone was sufficient, and
        # measured over five seeds the pair fixed 24 of 25 playbook orderings
        # without costing held-out ROC-AUC.
        #
        # Depth 5 was tried and rejected: no ordering improvement, and the
        # train/test gap started widening, which on a label that is Bernoulli by
        # construction means fitting noise.
        max_depth=4,
        subsample=0.9,
        random_state=seed,
    )

    return Pipeline([("prep", preprocessor), ("clf", classifier)])


#: The smallest ROC-AUC gain over the untrained lookup table that would justify
#: shipping a trained model at all. Set low deliberately: the point of printing the
#: comparison is to make the number visible, not to defend a threshold.
MIN_WORTHWHILE_GAIN = 0.005


def lookup_table_scores(frame: pd.DataFrame) -> np.ndarray:
    """
    Score every row with two dictionary lookups multiplied together.

    ``CATEGORY_BASE_RATE[category] * STRATEGY_FIT[(category, strategy)]`` -- no
    training, no features beyond those two, about two lines of code.

    This is the baseline that actually matters, and it is deliberately unkind to
    the model. The labels in this dataset are *generated* from those same tables
    plus adjustments for attempt number, customer quality, recency and amount, so
    a learner fitted to them is in large part re-deriving something already
    written down. If the trained model cannot beat the table by a margin worth the
    dependency, the honest conclusion is to ship the table.

    Reporting it means nobody has to take the model's value on trust -- including
    an interviewer, who will think of this comparison whether or not it is in the
    report. The number it produces is discussed in docs/05-ML-MODEL.md.
    """
    return np.array(
        [
            CATEGORY_BASE_RATE[FailureCategory(row.failure_category)]
            * STRATEGY_FIT.get(
                (
                    FailureCategory(row.failure_category),
                    RecoveryStrategy(row.proposed_strategy),
                ),
                DEFAULT_STRATEGY_FIT,
            )
            for row in frame.itertuples()
        ]
    )


def build_baseline(seed: int) -> Pipeline:
    """
    The comparison model: one shallow decision tree, same preprocessing.

    Args:
        seed: Fixed for reproducibility.

    Returns:
        An unfitted ``Pipeline`` using the identical ``prep`` step, so the
        comparison isolates the classifier and nothing else.
    """
    pipeline = build_pipeline(seed)
    # Depth 5 is roughly the complexity a person would hand-write as an if/else
    # ladder, which is precisely the thing the ensemble has to beat to justify
    # itself.
    pipeline.set_params(clf=DecisionTreeClassifier(max_depth=5, random_state=seed))
    return pipeline


def _score_model(pipeline: Pipeline, x_test: Any, y_test: Any) -> dict[str, float]:
    """
    Compute the standard classification metrics at ``DECISION_THRESHOLD``.

    Args:
        pipeline: A fitted pipeline.
        x_test: Held-out features.
        y_test: Held-out labels.

    Returns:
        A dict of plain Python floats, ready for JSON.
    """
    probabilities = pipeline.predict_proba(x_test)[:, 1]
    predictions = (probabilities >= DECISION_THRESHOLD).astype(int)

    return {
        "accuracy": float(accuracy_score(y_test, predictions)),
        # zero_division=0 rather than letting sklearn warn: if a model predicts
        # no positives at all, precision is undefined, and reporting it as 0.0 is
        # the interpretation that will not flatter a degenerate model.
        "precision": float(precision_score(y_test, predictions, zero_division=0)),
        "recall": float(recall_score(y_test, predictions, zero_division=0)),
        "f1": float(f1_score(y_test, predictions, zero_division=0)),
        # ROC-AUC is threshold-free, which is why it is the number used for model
        # selection here: the operating threshold is a policy decision made in
        # config.py, so the model should be chosen on its ranking quality rather
        # than on its behaviour at an arbitrary cut point.
        "roc_auc": float(roc_auc_score(y_test, probabilities)),
    }


def _feature_importances(pipeline: Pipeline) -> dict[str, float]:
    """
    Pair the classifier's importances with readable feature names.

    ``feature_importances_`` is a bare array whose positions mean nothing without
    the transformer that produced them. The names are pulled from the fitted
    ``OneHotEncoder`` and concatenated with the numeric column names in the same
    order the ``ColumnTransformer`` emits them ("cat" is declared before "num"),
    which is what makes the mapping correct rather than merely plausible.

    Args:
        pipeline: A fitted pipeline.

    Returns:
        The ``TOP_FEATURES`` most important features, highest first.
    """
    preprocessor: ColumnTransformer = pipeline.named_steps["prep"]
    encoder: OneHotEncoder = preprocessor.named_transformers_["cat"]

    names = list(encoder.get_feature_names_out(CATEGORICAL_FEATURES)) + list(NUMERIC_FEATURES)
    importances = pipeline.named_steps["clf"].feature_importances_

    ranked = sorted(zip(names, importances), key=lambda pair: pair[1], reverse=True)
    return {str(name): round(float(value), 4) for name, value in ranked[:TOP_FEATURES]}


#: Rows of synthetic data used for training.
#:
#: Sized for counterfactual coverage rather than for headline accuracy: the
#: predictor is queried on (category, strategy) combinations the playbook would
#: never propose, so the rare cells need enough rows to be learnable. See the
#: commentary on ``PLAYBOOK_SAMPLING_RATE`` in ``app.ml.dataset``. Training still
#: completes in well under a minute on a laptop.
DEFAULT_N_SAMPLES: int = 18000


def train_and_save(
    n_samples: int = DEFAULT_N_SAMPLES,
    seed: int = 42,
    out_dir: Path = MODELS_DIR,
) -> dict[str, Any]:
    """
    Generate data, fit both models, score them, and write the artefacts.

    Args:
        n_samples: Rows of synthetic data to generate.
        seed: Seed for the data generator, the split and the classifiers, so the
            whole run is reproducible end to end.
        out_dir: Directory to write ``propensity_model.joblib`` and
            ``metrics.json`` into.

    Returns:
        The metrics dict, identical to what is written to ``metrics.json``.
    """
    dataframe = generate_dataset(n_samples=n_samples, seed=seed)
    features = dataframe[FEATURE_COLUMNS]
    labels = dataframe[TARGET]

    x_train, x_test, y_train, y_test = train_test_split(
        features,
        labels,
        test_size=TEST_SIZE,
        # Stratified because the positive class is the minority here; an
        # unstratified split can hand the test set a materially different class
        # balance from the training set and make the metrics unreproducible.
        stratify=labels,
        random_state=seed,
    )

    model = build_pipeline(seed)
    model.fit(x_train, y_train)
    metrics = _score_model(model, x_test, y_test)

    baseline = build_baseline(seed)
    baseline.fit(x_train, y_train)
    baseline_metrics = _score_model(baseline, x_test, y_test)

    # The untrained table, scored on the identical held-out rows.
    lookup_auc = float(roc_auc_score(y_test, lookup_table_scores(x_test)))

    # Cross-validation runs on the training split only. Running it on the full
    # dataset would let the held-out test set influence model selection, which
    # quietly turns the final numbers into training-set numbers.
    cv_scores = cross_val_score(
        build_pipeline(seed),
        x_train,
        y_train,
        cv=StratifiedKFold(n_splits=5, shuffle=True, random_state=seed),
        scoring="roc_auc",
    )

    probabilities = model.predict_proba(x_test)[:, 1]
    predictions = (probabilities >= DECISION_THRESHOLD).astype(int)
    matrix = [[int(value) for value in row] for row in confusion_matrix(y_test, predictions)]

    # Recall at the threshold the guardrail engine actually enforces. Read from
    # config rather than hard-coded, so this number cannot drift away from the
    # limit that runs in production.
    policy_floor = float(get_settings().min_propensity_score)
    floor_predictions = (probabilities >= policy_floor).astype(int)

    model_version = f"v1-{ALGORITHM}-{n_samples}"

    # Stamp the version onto the artefact itself. Without it the only record of
    # which model is loaded lives in a separate JSON file that can be deleted,
    # replaced or copied from another run -- and "which model produced this
    # score?" is an audit question, not a curiosity.
    model.recoverai_model_version = model_version  # type: ignore[attr-defined]

    report: dict[str, Any] = {
        "model_version": model_version,
        "trained_at": utcnow().isoformat(),
        "algorithm": ALGORITHM,
        "n_samples": int(n_samples),
        "n_train": int(len(x_train)),
        "n_test": int(len(x_test)),
        "seed": int(seed),
        "positive_rate": round(float(labels.mean()), 4),
        **{key: round(value, 4) for key, value in metrics.items()},
        "cv_roc_auc_mean": round(float(cv_scores.mean()), 4),
        "cv_roc_auc_std": round(float(cv_scores.std()), 4),
        # What the trained model is actually worth over the simplest thing that
        # could work. Recorded as a number, every run, so the claim cannot drift
        # from the artefact.
        "lookup_table_baseline": {
            "description": "CATEGORY_BASE_RATE x STRATEGY_FIT, untrained",
            "roc_auc": round(lookup_auc, 4),
            "gradient_boosting_gain": round(metrics["roc_auc"] - lookup_auc, 4),
        },
        "baseline": {
            "algorithm": BASELINE_ALGORITHM,
            "max_depth": 5,
            **{key: round(value, 4) for key, value in baseline_metrics.items()},
        },
        "feature_importances": _feature_importances(model),
        "confusion_matrix": matrix,
        "threshold": DECISION_THRESHOLD,
        # The recall story, in numbers rather than adjectives.
        "policy_floor": policy_floor,
        "recall_at_policy_floor": round(
            float(recall_score(y_test, floor_predictions, zero_division=0)), 4
        ),
        "precision_at_policy_floor": round(
            float(precision_score(y_test, floor_predictions, zero_division=0)), 4
        ),
        "screened_out_at_policy_floor": int((probabilities < policy_floor).sum()),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out_dir / MODEL_FILENAME)
    # encoding is stated explicitly: Windows defaults text files to cp1252, and
    # an unstated encoding is how a JSON file ends up unreadable on a different
    # machine from the one that wrote it.
    (out_dir / METRICS_FILENAME).write_text(json.dumps(report, indent=2), encoding="utf-8")

    return report


# ---------------------------------------------------------------------------
# Console report
# ---------------------------------------------------------------------------
#
# Everything printed below is plain ASCII. The default Windows console code page
# is cp1252, which cannot encode box-drawing characters, the rupee sign or an
# en dash -- printing one raises UnicodeEncodeError and kills the training run at
# the very last step, after all the work is done. Hence "+---+" and "Rs".


def _rule(widths: tuple[int, ...]) -> str:
    """Build a horizontal ASCII rule matching the given column widths."""
    return "+" + "+".join("-" * (width + 2) for width in widths) + "+"


def _print_report(metrics: dict[str, Any]) -> None:
    """
    Print a readable metrics table to stdout.

    Args:
        metrics: The dict returned by :func:`train_and_save`.
    """
    widths = (26, 12, 12)
    rule = _rule(widths)
    baseline = metrics["baseline"]

    print()
    print(f"RecoverAI propensity model  --  {metrics['model_version']}")
    print(f"trained_at   : {metrics['trained_at']}")
    print(
        f"samples      : {metrics['n_samples']} "
        f"(train {metrics['n_train']} / test {metrics['n_test']}), "
        f"positive rate {metrics['positive_rate']:.3f}"
    )
    print()

    print(rule)
    print(f"| {'Metric':<26} | {'GradBoost':>12} | {'Tree d=5':>12} |")
    print(rule)
    for key in ("accuracy", "precision", "recall", "f1", "roc_auc"):
        print(f"| {key:<26} | {metrics[key]:>12.4f} | {baseline[key]:>12.4f} |")
    print(rule)
    print()

    print(
        f"5-fold CV ROC-AUC (train split) : "
        f"{metrics['cv_roc_auc_mean']:.4f} +/- {metrics['cv_roc_auc_std']:.4f}"
    )
    print()

    # The comparison that decides whether the model deserves to exist.
    lookup = metrics["lookup_table_baseline"]
    gain = lookup["gradient_boosting_gain"]
    print("What the trained model is worth over the simplest thing that could work:")
    print(f"  untrained lookup table (base rate x strategy fit) : {lookup['roc_auc']:.4f}")
    print(f"  trained gradient boosting                         : {metrics['roc_auc']:.4f}")
    print(f"  gain                                              : {gain:+.4f} ROC-AUC")
    if gain < MIN_WORTHWHILE_GAIN:
        print(f"  -> Below the {MIN_WORTHWHILE_GAIN:+.4f} bar. On this data the table is the")
        print("     honest answer; the model is not paying for its dependency.")
    else:
        print("     The model learns the attempt, recency and customer-history effects")
        print("     that two dictionary lookups cannot express. Note that the labels are")
        print("     generated from those same tables, so this gap is the ceiling of what")
        print("     synthetic data can demonstrate -- see docs/05-ML-MODEL.md.")
    print()

    (true_neg, false_pos), (false_neg, true_pos) = metrics["confusion_matrix"]
    print(f"Confusion matrix at threshold {metrics['threshold']}:")
    print(f"  true negatives : {true_neg:>5}    false positives: {false_pos:>5}")
    print(f"  false negatives: {false_neg:>5}    true positives : {true_pos:>5}")
    print()

    print(f"At the operating floor min_propensity_score = {metrics['policy_floor']}:")
    print(f"  recall     : {metrics['recall_at_policy_floor']:.4f}")
    print(f"  precision  : {metrics['precision_at_policy_floor']:.4f}")
    print(f"  screened out: {metrics['screened_out_at_policy_floor']} of {metrics['n_test']} cases")
    print(
        "  A false negative costs the whole transaction; a false positive costs "
        "one gateway call."
    )
    print()

    print(f"Top {len(metrics['feature_importances'])} features:")
    for name, value in metrics["feature_importances"].items():
        bar = "#" * max(1, int(round(value * 60)))
        print(f"  {name:<44} {value:6.4f}  {bar}")
    print()


def main() -> None:
    """Entry point for ``python -m app.ml.train``."""
    parser = argparse.ArgumentParser(description="Train the RecoverAI propensity model.")
    # Default deliberately mirrors train_and_save's own default rather than
    # restating a number. A CLI flag that silently disagrees with the function it
    # calls is how a "tuned" configuration quietly stops being the one that ships.
    parser.add_argument(
        "--samples",
        type=int,
        default=DEFAULT_N_SAMPLES,
        help=f"rows of synthetic data (default {DEFAULT_N_SAMPLES})",
    )
    parser.add_argument("--seed", type=int, default=42, help="seed for a reproducible run")
    args = parser.parse_args()

    metrics = train_and_save(n_samples=args.samples, seed=args.seed)
    _print_report(metrics)
    print(f"Saved model   -> {MODELS_DIR / MODEL_FILENAME}")
    print(f"Saved metrics -> {MODELS_DIR / METRICS_FILENAME}")


if __name__ == "__main__":
    main()
