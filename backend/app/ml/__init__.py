"""
The recovery-propensity model: "if we attempt this recovery, will it collect?"

The package answers exactly one question and answers it as a number in [0, 1].
That number feeds two places, and nowhere else:

*   ``R10_PROPENSITY_FLOOR`` in the guardrail engine, which refuses to spend a
    gateway call on a payment the model considers hopeless.
*   The approval screen, where the operator sees the score together with the
    reasons behind it, so a human is judging evidence rather than a bare digit.

Module map
----------
``features.py``   The single definition of a model row. Both training and
                  serving build rows through it, so the two cannot drift.
``dataset.py``    A documented synthetic generative process. No public dataset
                  of Razorpay recovery outcomes exists; rather than quote a
                  number nobody can check, the assumptions are written down as
                  code a reviewer can read and argue with.
``train.py``      ``python -m app.ml.train`` -- fits the pipeline, compares it
                  against a decision-tree baseline, and writes both the artefact
                  and its metrics to ``backend/models/``.
``predictor.py``  The serving front door. Loads the artefact if it exists and
                  falls back to a documented heuristic if it does not, so the
                  application is fully runnable before anyone trains anything.

This ``__init__`` deliberately re-exports nothing. Importing a submodule here
would make ``import app.ml.features`` pay the cost of importing pandas and
scikit-learn, and ``features.py`` is the one module in the package that has no
third-party dependencies at all -- a property worth keeping.
"""
