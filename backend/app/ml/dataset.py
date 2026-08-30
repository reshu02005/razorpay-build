"""
The synthetic training set for the recovery-propensity model -- and the written
record of every assumption behind it.

**This data is synthetic. It is not real Razorpay data and it is not sampled
from any public dataset.** That is stated first because it is the most important
thing a reviewer needs to know about the model's numbers.

Why synthetic
-------------
Recovery outcomes are commercially sensitive. No payment processor publishes
"we retried N failed payments of category X with strategy Y and Z% collected",
and a merchant's own history is exactly the asset they will not hand to a
student project. The honest options were therefore:

1.  Ship no model, and score propensity with an if/else ladder.
2.  Copy plausible-looking numbers from a blog post and present them as fact.
3.  Write the generative process down as executable, commented code so that a
    reviewer can audit the assumptions instead of taking a number on faith.

Option 3 is what this module is. Every probability below is a stated belief
about how Indian payment failures behave, with the reasoning attached, and every
one of them is a single edit away from being replaced by a merchant's real
numbers. The learned model is a compression of these assumptions -- it is worth
exactly as much as they are, and no more. Saying so plainly is the point.

Why the labels are drawn, not thresholded
-----------------------------------------
The generator computes a probability and then draws the label with
``rng.random() < p``. A deterministic ``p > 0.5`` rule would make the mapping
from features to label a pure function that a gradient-boosted tree can memorise
almost perfectly, and the resulting 0.99 ROC-AUC would be a measurement of the
threshold, not of the model. Bernoulli sampling produces a learnable but
irreducibly noisy signal, so the reported metrics mean what metrics normally
mean. A small unobserved multiplier (see ``LATENT_NOISE_SIGMA``) is layered on
top to represent everything the feature row cannot see -- the issuer's mood that
morning, whether the customer's phone had signal -- which puts a genuine ceiling
below 1.0 on any achievable score.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from app.domain.enums import FailureCategory, PaymentMethod, RecoveryStrategy
from app.ml.features import FEATURE_COLUMNS, TARGET, build_feature_row

# ---------------------------------------------------------------------------
# Ground truth #1 -- how recoverable each failure category is
# ---------------------------------------------------------------------------

#: Probability that a *typical* recovery attempt on a first try, for an
#: average-quality customer and an average-sized payment, would collect.
#:
#: This is the category's centre of gravity, not its ceiling. The strategy
#: multipliers in the next table move it up or down: the best-fit strategy for a
#: category lands meaningfully above its base rate and a poor-fit one lands far
#: below, which is exactly the spread the model has to learn.
#:
#: The bands, and why each category sits in the one it does:
#:
#: * ~0.75 -- **nothing was wrong with the customer or the instrument.** The
#:   payment failed on our side of the fence. Re-presenting it is the textbook
#:   case for automated recovery.
#: * ~0.60 -- **intent existed but the window closed.** The customer meant to
#:   pay and did not finish. Winning them back is likely but not automatic.
#: * ~0.45 -- **the customer hit a step they could not complete.** Some will
#:   manage it second time, many will not.
#: * ~0.20-0.32 -- **a real constraint blocked the payment.** The issuer said no,
#:   or the money was not there, or the card is dead. Only a change of rail or a
#:   change of timing helps, and often neither does.
#: * ~0.02 -- **we must not try.** Risk-blocked transactions are excluded by
#:   guardrail R3 before the model is even consulted; the near-zero rate here
#:   exists so that if the guardrail were ever removed, the model would still
#:   refuse on its own.
CATEGORY_BASE_RATE: dict[FailureCategory, float] = {
    # Razorpay or the acquirer faulted. The customer did nothing wrong and the
    # instrument is fine, so the same payment usually goes through on a retry.
    FailureCategory.GATEWAY_ERROR: 0.75,
    # Same transient character as a gateway error. Marginally lower because a
    # dropped connection can hide a genuine issuer decline that never reached us,
    # so a slice of these are really bank declines wearing a disguise.
    FailureCategory.NETWORK_ERROR: 0.74,
    # The collect request expired unanswered. Usually the customer simply did not
    # open the app in time; a fresh request often lands. Lower than a gateway
    # fault because some of these are silent refusals.
    FailureCategory.UPI_TIMEOUT: 0.62,
    # Checkout window closed. Intent was real -- they were on the page -- but time
    # has passed and some of that intent has cooled.
    FailureCategory.SESSION_EXPIRED: 0.58,
    # OTP or 3-D Secure not completed. A coin-flip: some customers mistyped the
    # OTP and will get it right; others never received it and will fail again on
    # the identical rail.
    FailureCategory.AUTHENTICATION_FAILED: 0.45,
    # The issuer refused. Re-presenting the same card to the same issuer mostly
    # earns the same answer, so what recovery there is comes almost entirely from
    # switching rails.
    FailureCategory.BANK_DECLINE: 0.32,
    # The customer walked away from checkout. No technical fault to fix -- this is
    # a persuasion problem, and most of the time the answer stays no.
    FailureCategory.CUSTOMER_ABANDONED: 0.30,
    # We could not classify the failure. Set at the rough population average
    # rather than at zero: absence of evidence is not evidence of failure. (It is
    # also not evidence of safety, which is why guardrail R3 blocks it anyway.)
    FailureCategory.UNKNOWN: 0.25,
    # No money in the account. Retrying now cannot work by definition; the only
    # thing that helps is waiting, which is why the strategy multiplier matters
    # more here than for any other category.
    FailureCategory.INSUFFICIENT_FUNDS: 0.22,
    # Expired or mistyped card. The instrument itself is dead, so anything that
    # reuses it is wasted; recovery means getting the customer onto a different
    # instrument entirely.
    FailureCategory.INVALID_INSTRUMENT: 0.18,
    # Flagged by a risk engine. Near zero on purpose: a "successful" recovery here
    # is plausibly a chargeback in three weeks, which is worse than no recovery.
    FailureCategory.RISK_BLOCKED: 0.02,
}

# ---------------------------------------------------------------------------
# Ground truth #2 -- how well each strategy fits each failure category
# ---------------------------------------------------------------------------
#
# This table is the thesis of the whole product in numeric form: the right
# recovery action depends on *why* the payment failed. A multiplier above 1.0
# means the strategy beats the category's average; below 1.0 means it wastes the
# attempt. If every row here were 1.0, the product would have no reason to exist.

#: Multipliers for the strategies that put a payment on a rail (or deliberately
#: delay one). Keyed category -> strategy.
_ATTEMPTING_STRATEGY_FIT: dict[FailureCategory, dict[RecoveryStrategy, float]] = {
    FailureCategory.BANK_DECLINE: {
        # Same card, same issuer, same answer.
        RecoveryStrategy.RETRY_SAME_METHOD: 0.55,
        # The single best move in this table: UPI goes through a completely
        # different authorisation path, so an issuer's card-side decision does
        # not follow it there.
        RecoveryStrategy.SWITCH_TO_UPI: 1.25,
        RecoveryStrategy.SWITCH_TO_NETBANKING: 1.10,
        # A second card might work, but we have no evidence the customer has one.
        RecoveryStrategy.SWITCH_TO_CARD: 0.70,
        RecoveryStrategy.RETRY_LATER: 0.75,
    },
    FailureCategory.INSUFFICIENT_FUNDS: {
        # The balance does not change because we asked twice.
        RecoveryStrategy.RETRY_SAME_METHOD: 0.60,
        # Waiting for payday is the textbook answer, and it is the reason
        # RETRY_LATER exists as a strategy at all.
        RecoveryStrategy.RETRY_LATER: 1.35,
        # A credit line is a different pocket from a bank balance.
        RecoveryStrategy.SWITCH_TO_CARD: 0.95,
        # UPI usually draws on the same empty account.
        RecoveryStrategy.SWITCH_TO_UPI: 0.80,
        RecoveryStrategy.SWITCH_TO_NETBANKING: 0.70,
    },
    FailureCategory.UPI_TIMEOUT: {
        RecoveryStrategy.RETRY_SAME_METHOD: 1.20,
        # For a UPI timeout the original method already is UPI, so this is the
        # same action under a different name; it scores the same on purpose.
        RecoveryStrategy.SWITCH_TO_UPI: 1.20,
        RecoveryStrategy.SWITCH_TO_CARD: 0.85,
        RecoveryStrategy.SWITCH_TO_NETBANKING: 0.75,
        # Intent is perishable: an unanswered collect request answered tomorrow
        # is worth much less than one answered in ten minutes.
        RecoveryStrategy.RETRY_LATER: 0.80,
    },
    FailureCategory.SESSION_EXPIRED: {
        # Nothing was broken except the clock, so simply asking again is right.
        RecoveryStrategy.RETRY_SAME_METHOD: 1.25,
        RecoveryStrategy.SWITCH_TO_UPI: 1.05,
        RecoveryStrategy.SWITCH_TO_CARD: 1.00,
        RecoveryStrategy.SWITCH_TO_NETBANKING: 0.95,
        RecoveryStrategy.RETRY_LATER: 0.70,
    },
    FailureCategory.GATEWAY_ERROR: {
        RecoveryStrategy.RETRY_SAME_METHOD: 1.20,
        # Switching rails is not wrong, just unnecessary -- every extra decision
        # we push onto the customer is another place to lose them.
        RecoveryStrategy.SWITCH_TO_UPI: 1.00,
        RecoveryStrategy.SWITCH_TO_CARD: 0.95,
        RecoveryStrategy.SWITCH_TO_NETBANKING: 0.90,
        RecoveryStrategy.RETRY_LATER: 0.90,
    },
    FailureCategory.NETWORK_ERROR: {
        RecoveryStrategy.RETRY_SAME_METHOD: 1.20,
        RecoveryStrategy.SWITCH_TO_UPI: 1.00,
        RecoveryStrategy.SWITCH_TO_CARD: 0.95,
        RecoveryStrategy.SWITCH_TO_NETBANKING: 0.90,
        RecoveryStrategy.RETRY_LATER: 0.90,
    },
    FailureCategory.AUTHENTICATION_FAILED: {
        # A UPI PIN is entered in the customer's own app, which they can already
        # open, rather than on an issuer's OTP page that may never load.
        RecoveryStrategy.SWITCH_TO_UPI: 1.30,
        RecoveryStrategy.RETRY_SAME_METHOD: 0.90,
        RecoveryStrategy.SWITCH_TO_NETBANKING: 0.80,
        RecoveryStrategy.SWITCH_TO_CARD: 0.70,
        RecoveryStrategy.RETRY_LATER: 0.70,
    },
    FailureCategory.INVALID_INSTRUMENT: {
        # The clearest bad fit in the table, and the one the model most needs to
        # learn: re-presenting a dead card cannot work no matter how good the
        # customer is or how small the amount.
        RecoveryStrategy.RETRY_SAME_METHOD: 0.25,
        RecoveryStrategy.RETRY_LATER: 0.35,
        RecoveryStrategy.SWITCH_TO_UPI: 1.40,
        RecoveryStrategy.SWITCH_TO_NETBANKING: 1.20,
        # A different card, if they have one.
        RecoveryStrategy.SWITCH_TO_CARD: 1.10,
    },
    FailureCategory.CUSTOMER_ABANDONED: {
        # Fewer steps means less to abandon.
        RecoveryStrategy.SWITCH_TO_UPI: 1.20,
        RecoveryStrategy.RETRY_SAME_METHOD: 1.00,
        RecoveryStrategy.RETRY_LATER: 0.90,
        RecoveryStrategy.SWITCH_TO_CARD: 0.85,
        RecoveryStrategy.SWITCH_TO_NETBANKING: 0.75,
    },
    FailureCategory.UNKNOWN: {
        # We do not know what failed, so we have no basis for preferring any
        # rail. Flat multipliers say exactly that instead of inventing a
        # preference the data cannot support.
        RecoveryStrategy.RETRY_SAME_METHOD: 0.95,
        RecoveryStrategy.SWITCH_TO_UPI: 0.95,
        RecoveryStrategy.SWITCH_TO_CARD: 0.90,
        RecoveryStrategy.SWITCH_TO_NETBANKING: 0.90,
        RecoveryStrategy.RETRY_LATER: 0.85,
    },
    FailureCategory.RISK_BLOCKED: {
        # Uniformly poor. The base rate of 0.02 already does the work; keeping
        # these flat avoids implying that some clever rail choice makes a
        # fraud-flagged transaction collectable.
        RecoveryStrategy.RETRY_SAME_METHOD: 0.30,
        RecoveryStrategy.SWITCH_TO_UPI: 0.30,
        RecoveryStrategy.SWITCH_TO_CARD: 0.30,
        RecoveryStrategy.SWITCH_TO_NETBANKING: 0.30,
        RecoveryStrategy.RETRY_LATER: 0.30,
    },
}

#: Strategies that create no payment attempt at all, and therefore collect
#: nothing inside the window this label measures.
#:
#: They are not zero. ``MANUAL_REVIEW`` gets 0.10 because a human operator
#: occasionally does rescue the payment off-platform, and ``NO_RECOVERY`` gets
#: 0.02 because a customer very occasionally comes back unprompted. Setting them
#: to a hard 0.0 would let the model treat "recommend manual review" as a
#: guaranteed negative and learn to recognise the recommendation rather than the
#: situation.
_NON_ATTEMPTING_STRATEGY_FIT: dict[RecoveryStrategy, float] = {
    RecoveryStrategy.MANUAL_REVIEW: 0.10,
    RecoveryStrategy.NO_RECOVERY: 0.02,
}

#: Flattened (category, strategy) -> multiplier, covering every combination.
#: Built rather than written out as 77 literal lines because the two source
#: tables above are the things a reviewer should read, and a flat 77-entry
#: literal would bury them.
STRATEGY_FIT: dict[tuple[FailureCategory, RecoveryStrategy], float] = {
    (category, strategy): fit
    for category, per_strategy in _ATTEMPTING_STRATEGY_FIT.items()
    for strategy, fit in {**per_strategy, **_NON_ATTEMPTING_STRATEGY_FIT}.items()
}

#: Used when a (category, strategy) pair is somehow missing -- for instance if a
#: new enum member is added and this file is not updated. Neutral-but-slightly-
#: pessimistic, so a forgotten table entry degrades the score rather than
#: silently flattering an untested combination.
DEFAULT_STRATEGY_FIT: float = 0.85


# ---------------------------------------------------------------------------
# Ground truth #3 -- the continuous adjustments
# ---------------------------------------------------------------------------

#: Multiplier by attempt number. The drop from the first to the second attempt is
#: steep and deliberate: the first attempt already harvested the easy transient
#: failures, so whatever is left is structurally harder. This is also the
#: quantitative argument behind ``max_recovery_attempts = 2`` in the config -- by
#: the third attempt the expected value no longer justifies the customer friction.
ATTEMPT_FACTOR: dict[int, float] = {1: 1.00, 2: 0.62, 3: 0.40}

#: Anything beyond the tabulated attempts. Guardrail R1 stops us long before
#: here; the value exists so the function is total.
ATTEMPT_FACTOR_BEYOND: float = 0.30

#: Customer-quality multiplier is linear in the customer's true payment success
#: rate and centred on 0.5, so a customer with no history (whose rate is reported
#: as the neutral 0.5) gets a multiplier of exactly 1.0 and is neither rewarded
#: nor punished for being new.
CUSTOMER_FACTOR_AT_ZERO: float = 0.65
CUSTOMER_FACTOR_SLOPE: float = 0.70

#: Value decay. Halves the distance to the floor roughly every 25 hours: intent
#: is perishable, and a customer who failed a payment four days ago has usually
#: either bought elsewhere or forgotten.
TIME_DECAY_FLOOR: float = 0.55
TIME_DECAY_HOURS: float = 36.0

#: Upper bound on the age we generate, matched to ``settings.max_payment_age_hours``
#: (R11 refuses anything older). Training past the policy horizon would fit a
#: region of the feature space the guardrails never let the model see.
MAX_HOURS_SINCE_FAILURE: float = 168.0

#: Amount range, in whole rupees. The ceiling matches
#: ``settings.max_recovery_amount_paise`` (Rs 50,000) for the same reason.
MIN_AMOUNT_RUPEES: float = 100.0
MAX_AMOUNT_RUPEES: float = 50_000.0

#: Share of generated payments drawn from the high-value component. Chosen so the
#: Rs 10,000 review threshold and the Rs 50,000 ceiling both sit inside
#: well-populated regions rather than in the extrapolation tail.
HIGH_VALUE_SHARE: float = 0.18

#: Share of generated rows drawn uniformly across the whole R11 window rather
#: than from the "decided within hours" exponential. Gives the model real support
#: across the entire age range it is permitted to operate on, instead of only the
#: first few hours where the exponential puts nearly all of its mass.
BACKLOG_SHARE: float = 0.35

#: Spread of the unobserved multiplier. Small enough that the documented signal
#: still dominates, large enough that no model can reach a perfect score.
LATENT_NOISE_SIGMA: float = 0.12

#: Probabilities are clamped into this band before the coin is flipped. Nothing
#: in payments is ever certain in either direction.
MIN_PROBABILITY: float = 0.01
MAX_PROBABILITY: float = 0.97


# ---------------------------------------------------------------------------
# Sampling distributions
# ---------------------------------------------------------------------------

#: Relative frequency of each failure category in the generated population.
#: Shaped to look like an Indian mid-market checkout: declines and UPI timeouts
#: dominate, gateway faults are a steady minority, fraud blocks are rare. The
#: mix affects the class balance and therefore every reported metric, which is
#: why it is written here rather than left implicit in a uniform draw.
CATEGORY_FREQUENCY: dict[FailureCategory, float] = {
    FailureCategory.BANK_DECLINE: 0.20,
    FailureCategory.UPI_TIMEOUT: 0.15,
    FailureCategory.INSUFFICIENT_FUNDS: 0.13,
    FailureCategory.AUTHENTICATION_FAILED: 0.11,
    FailureCategory.SESSION_EXPIRED: 0.10,
    FailureCategory.GATEWAY_ERROR: 0.08,
    FailureCategory.NETWORK_ERROR: 0.07,
    FailureCategory.CUSTOMER_ABANDONED: 0.06,
    FailureCategory.INVALID_INSTRUMENT: 0.06,
    FailureCategory.RISK_BLOCKED: 0.02,
    FailureCategory.UNKNOWN: 0.02,
}

#: Default instrument mix. ``UNKNOWN`` is included with a small share on purpose:
#: production really does produce payments with an unmapped method, and if the
#: encoder never saw that token at fit time those rows would be encoded as an
#: all-zero block -- the feature would vanish silently rather than fail loudly.
_DEFAULT_METHOD_MIX: dict[PaymentMethod, float] = {
    PaymentMethod.UPI: 0.42,
    PaymentMethod.CARD: 0.36,
    PaymentMethod.NETBANKING: 0.12,
    PaymentMethod.WALLET: 0.06,
    PaymentMethod.EMI: 0.02,
    PaymentMethod.UNKNOWN: 0.02,
}

#: Some categories all but determine the instrument. Drawing the method
#: independently of the category would put "UPI timeout on a netbanking payment"
#: rows into training -- combinations that cannot occur -- and the model would
#: waste capacity learning a correlation that reality already guarantees.
_METHOD_MIX_OVERRIDES: dict[FailureCategory, dict[PaymentMethod, float]] = {
    # A collect request only exists on UPI.
    FailureCategory.UPI_TIMEOUT: {PaymentMethod.UPI: 1.00},
    # "Card expired / wrong number" is a card-shaped failure; EMI rides on cards.
    FailureCategory.INVALID_INSTRUMENT: {PaymentMethod.CARD: 0.90, PaymentMethod.EMI: 0.10},
    # OTP and 3-D Secure are card and netbanking phenomena; UPI has its own PIN
    # flow that fails differently.
    FailureCategory.AUTHENTICATION_FAILED: {
        PaymentMethod.CARD: 0.75,
        PaymentMethod.NETBANKING: 0.15,
        PaymentMethod.UPI: 0.10,
    },
}

#: Share of rows whose strategy is the best one for the category. The remainder
#: are drawn uniformly across all seven strategies.
#:
#: The alternative considered, and why it lost: sampling *only* the best strategy
#: would mirror deployment (the planner does propose the playbook answer) but
#: would leave the model no counterfactual evidence -- it would never once see
#: "retry the same dead card" and so could never learn that it fails. Sampling
#: uniformly would give perfect coverage but a training distribution that looks
#: nothing like production. The mixture buys coverage of the bad choices while
#: keeping the good ones dominant, which is the same trade-off an exploration
#: rate makes in a bandit.
#:
#: The value is 0.45 rather than something closer to production because of how
#: the model is actually *queried*. The predictor is not asked "will this
#: recovery succeed?" on rows drawn from the deployed policy -- it is asked a
#: counterfactual: "how likely is *this* strategy to work for *this* failure?",
#: including strategies the playbook would never choose. A logging policy that
#: almost always follows the playbook cannot answer that question, because the
#: interesting cells are nearly empty.
#:
#: At 0.70 the bank-decline x retry-same-method cell held about 50 rows out of
#: 6,000, and the model could not separate that interaction from the strong global
#: association between "retry the same method" and the easy transient categories.
#: The consequence was concrete and wrong: it scored re-presenting a declined card
#: *above* switching the customer to UPI, which is precisely the wasted retry this
#: whole system exists to prevent. Raising exploration and the sample count fixed
#: the ordering and, measured over five seeds, also raised held-out ROC-AUC
#: (0.782 -> 0.809) and tightened its variance -- better coverage generalises
#: better, so there was no accuracy/validity trade-off to make.
#:
#: `tests/test_ml_predictor.py` pins the orderings that matter, so this cannot
#: silently regress.
PLAYBOOK_SAMPLING_RATE: float = 0.45

#: Distribution over attempt numbers: most cases are on their first attempt,
#: because guardrail R1 caps the case at two and many first attempts succeed.
_ATTEMPT_DISTRIBUTION: dict[int, float] = {1: 0.75, 2: 0.20, 3: 0.05}


def _best_strategy(category: FailureCategory) -> RecoveryStrategy:
    """
    The highest-fit strategy for a category, read straight out of STRATEGY_FIT.

    Derived rather than declared in a second table, so the "preferred strategy"
    used for sampling cannot drift away from the multipliers that decide whether
    it actually works.
    """
    candidates = _ATTEMPTING_STRATEGY_FIT[category]
    return max(candidates, key=lambda strategy: candidates[strategy])


#: Cached because it is looked up once per generated row.
BEST_STRATEGY: dict[FailureCategory, RecoveryStrategy] = {
    category: _best_strategy(category) for category in _ATTEMPTING_STRATEGY_FIT
}

_ALL_STRATEGIES: tuple[RecoveryStrategy, ...] = tuple(RecoveryStrategy)


# ---------------------------------------------------------------------------
# The generative process, exposed so it can be audited and unit-tested
# ---------------------------------------------------------------------------


def strategy_fit(category: FailureCategory, strategy: RecoveryStrategy) -> float:
    """
    How well ``strategy`` suits ``category``, as a multiplier on the base rate.

    Args:
        category: The classified failure reason.
        strategy: The recovery strategy under consideration.

    Returns:
        A multiplier; >1.0 beats the category average, <1.0 wastes the attempt.
        Falls back to ``DEFAULT_STRATEGY_FIT`` for an untabulated pair.
    """
    return STRATEGY_FIT.get((category, strategy), DEFAULT_STRATEGY_FIT)


def attempt_factor(attempt_number: int) -> float:
    """
    Multiplier for how much a repeat attempt is worth.

    Args:
        attempt_number: 1 for the first recovery attempt on a case.

    Returns:
        A multiplier in (0, 1]. Shared with the predictor's heuristic fallback so
        the two can never disagree about how much a second attempt is worth.
    """
    return ATTEMPT_FACTOR.get(int(attempt_number), ATTEMPT_FACTOR_BEYOND)


def customer_quality_factor(success_rate: float) -> float:
    """
    Multiplier for how reliably this customer pays.

    Args:
        success_rate: Share of the customer's payments that succeed, in [0, 1].
            A customer with no history is passed 0.5, which yields exactly 1.0.

    Returns:
        A multiplier, roughly 0.65 (never pays) to 1.35 (always pays). Shared
        with the predictor's heuristic fallback for the same reason as
        :func:`attempt_factor`.
    """
    clamped = min(1.0, max(0.0, float(success_rate)))
    return CUSTOMER_FACTOR_AT_ZERO + CUSTOMER_FACTOR_SLOPE * clamped


def success_probability(
    *,
    failure_category: FailureCategory,
    proposed_strategy: RecoveryStrategy,
    amount_rupees: float,
    attempt_number: int,
    customer_quality: float,
    hours_since_failure: float,
) -> float:
    """
    The documented ground-truth probability that a recovery attempt collects.

    This is deliberately public rather than buried inside the sampling loop: it
    *is* the assumption set, so it should be callable, testable and arguable on
    its own. The model learns to approximate this function through the fog of a
    Bernoulli draw and an unobserved multiplier.

    The terms multiply rather than add. Multiplication encodes "every one of
    these can independently kill the attempt", which matches how payments behave:
    a perfect strategy on a dead instrument still collects nothing. An additive
    model would let a strong customer history rescue an impossible case.

    Args:
        failure_category: Classified reason the original payment failed.
        proposed_strategy: The recovery strategy being evaluated.
        amount_rupees: Payment value in rupees (a modelling scale only).
        attempt_number: 1 for the first recovery attempt.
        customer_quality: The customer's *true* propensity to pay, in [0, 1].
            Note this is the latent value, not the observed success rate -- see
            :func:`generate_dataset`.
        hours_since_failure: Age of the original failure, in hours.

    Returns:
        A probability clamped into ``[MIN_PROBABILITY, MAX_PROBABILITY]``.
    """
    base = CATEGORY_BASE_RATE.get(failure_category, CATEGORY_BASE_RATE[FailureCategory.UNKNOWN])
    fit = strategy_fit(failure_category, proposed_strategy)

    # Higher-value payments recover slightly less often: bigger amounts hit
    # issuer velocity limits, exhaust balances and prompt more hesitation. The
    # effect is logarithmic, not linear -- the gap between Rs 100 and Rs 1,000
    # matters far more than the gap between Rs 40,000 and Rs 41,000.
    safe_amount = max(MIN_AMOUNT_RUPEES, float(amount_rupees))
    amount_factor = 1.05 - 0.075 * math.log10(safe_amount / MIN_AMOUNT_RUPEES)
    amount_factor = min(1.05, max(0.80, amount_factor))

    # Intent decays exponentially towards a floor rather than to zero, because a
    # small share of customers do come back a week later.
    decay = math.exp(-max(0.0, float(hours_since_failure)) / TIME_DECAY_HOURS)
    time_factor = TIME_DECAY_FLOOR + (1.0 - TIME_DECAY_FLOOR) * decay

    probability = (
        base
        * fit
        * amount_factor
        * attempt_factor(attempt_number)
        * customer_quality_factor(customer_quality)
        * time_factor
    )
    return min(MAX_PROBABILITY, max(MIN_PROBABILITY, probability))


# ---------------------------------------------------------------------------
# Sampling helpers
# ---------------------------------------------------------------------------


def _draw_from_mix(rng: np.random.Generator, mix: dict) -> object:
    """
    Draw one key from a ``{value: weight}`` mapping.

    Weights are normalised here rather than being required to sum to 1.0, so the
    tables above stay editable without a reviewer having to re-balance every
    other entry to change one.
    """
    keys = list(mix)
    weights = np.array([mix[key] for key in keys], dtype=float)
    weights = weights / weights.sum()
    # Draw an index rather than passing the objects to rng.choice: numpy would
    # otherwise build an object array of enum members on every single call.
    return keys[int(rng.choice(len(keys), p=weights))]


def _draw_amount_rupees(rng: np.random.Generator) -> float:
    """
    Draw a payment value.

    A two-component log-normal mixture, not a single log-normal.

    Real transaction values are log-normal in shape: a dense mass of small
    everyday purchases with a long tail of expensive ones. But a single
    log-normal centred on the everyday mass leaves the expensive tail with almost
    no support -- at median Rs 600 with sigma 1.0, fewer than two rows in 18,000
    land above Rs 24,000.

    That matters here specifically, because the amounts this model is *queried*
    on are not distributed like the amounts it would be trained on. The guardrail
    thresholds sit at Rs 10,000 (review) and Rs 50,000 (ceiling), so the cases a
    human actually looks at are concentrated exactly where a single log-normal has
    no data -- and a gradient-boosted tree asked to extrapolate there returns
    whatever its last split happened to say. It did: it scored a Rs 24,000 failure
    well above a Rs 199 one, inverting the documented relationship.

    The second component (median about Rs 9,900) is the high-value band. It is a
    minority of transactions, as it should be, but a well-populated one.
    """
    if rng.random() < HIGH_VALUE_SHARE:
        rupees = float(np.exp(rng.normal(9.2, 0.8)))   # median approx Rs 9,900
    else:
        rupees = float(np.exp(rng.normal(6.4, 1.0)))   # median approx Rs 600
    return float(min(MAX_AMOUNT_RUPEES, max(MIN_AMOUNT_RUPEES, rupees)))


def _draw_hours_since_failure(rng: np.random.Generator) -> float:
    """
    Draw how long ago the payment failed.

    A mixture, for the same reason as the amount draw: the model has to be valid
    across the entire window the guardrails permit, not only where the density is.

    A plain ``exponential(12)`` is the honest shape for *when decisions get made*
    -- most within a few hours -- but it puts roughly one row in twenty thousand
    beyond 120 hours. R11 allows recovery up to 168 hours, and the seeded demo
    spreads failures across a fortnight, so the model was being asked about a
    region containing effectively no training data. It answered by extrapolating,
    and got the sign wrong: a five-day-old failure scored *higher* than an
    hour-old one, exactly reversing the intent decay this module documents.

    The ``BACKLOG_SHARE`` component represents a real population anyway -- failures
    that sat in a queue over a weekend before anyone looked at them.

    The uniform component deliberately spans the **whole** window rather than
    starting where the exponential thins out. An earlier version started it at 24
    hours, which put a step change in the density at exactly that point; the
    trees found the step and split on it, and the learned response picked up a
    bump at 24 hours that nothing in the ground truth justifies. Overlapping the
    two components removes the artefact, because a boundary the generator does
    not smooth over is a boundary the model will happily learn.
    """
    if rng.random() < BACKLOG_SHARE:
        hours = float(rng.uniform(0.0, MAX_HOURS_SINCE_FAILURE))
    else:
        hours = float(rng.exponential(10.0))
    return float(min(MAX_HOURS_SINCE_FAILURE, max(0.0, hours)))


def _draw_customer(rng: np.random.Generator) -> tuple[int, float, float]:
    """
    Draw one customer's history and their latent payment quality.

    This is the most deliberate piece of the generator. Each customer has a
    hidden true quality ``q``; what the feature row gets to see is the *observed*
    success rate from a finite number of past payments, which is a noisy estimate
    of ``q``. The outcome is driven by ``q``.

    That gap is what makes ``customer_total_payments`` a real feature rather than
    a decorative one: 1-out-of-1 and 40-out-of-40 report the same rate but carry
    completely different amounts of evidence, and the correct response is to
    shrink a thin history back towards the population mean. That shrinkage is an
    interaction between two numeric features -- exactly the kind of structure a
    gradient-boosted tree can represent and a logistic regression cannot, which
    is a large part of why the tree ensemble is the chosen model.

    Returns:
        ``(total_payments, observed_success_rate, latent_quality)``.
    """
    total_payments = int(min(60, rng.gamma(shape=2.0, scale=4.0)))
    latent_quality = float(rng.beta(5.0, 3.0))  # mean approx 0.63

    if total_payments <= 0:
        # Matches ``Customer.prior_success_rate``, which returns the neutral 0.5
        # for a customer with no history. If training encoded "no history" as 0.0
        # while serving encoded it as 0.5, every brand-new customer would be
        # scored against a value the model had never seen.
        return 0, 0.5, latent_quality

    successes = int(rng.binomial(total_payments, latent_quality))
    return total_payments, successes / total_payments, latent_quality


def _draw_strategy(rng: np.random.Generator, category: FailureCategory) -> RecoveryStrategy:
    """Draw a proposed strategy: mostly the playbook answer, sometimes anything."""
    if rng.random() < PLAYBOOK_SAMPLING_RATE:
        return BEST_STRATEGY[category]
    return _ALL_STRATEGIES[int(rng.integers(len(_ALL_STRATEGIES)))]


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


def generate_dataset(n_samples: int = 18000, seed: int = 42) -> pd.DataFrame:
    """
    Generate the synthetic training set.

    Reproducible by construction: ``numpy.random.default_rng(seed)`` gives a
    fresh, independent bit generator rather than mutating the global numpy random
    state, so calling this function cannot perturb any other randomness in the
    process, and the same seed gives byte-identical data on Windows, macOS and
    Linux.

    Every row is built through :func:`app.ml.features.build_feature_row`, the
    same function the live predictor uses. That is not a convenience -- it is the
    guarantee that there is no training/serving skew to find.

    Args:
        n_samples: Number of rows to generate. 18,000 is chosen so that even the
            rare (category, strategy) cells -- the counterfactual ones the
            predictor is actually queried on -- carry a few hundred rows each,
            while training still finishes in well under a minute on a laptop.
        seed: Seed for the row generator.

    Returns:
        A DataFrame with columns ``FEATURE_COLUMNS + [TARGET]``, in that order.
    """
    rng = np.random.default_rng(seed)
    rows: list[dict] = []

    # A plain Python loop, not vectorised numpy. 6,000 iterations run in well
    # under a second, and the loop reads like the specification it implements --
    # a reviewer can follow "draw a category, then a method that fits it, then a
    # strategy" line by line. Vectorising would trade that away for time nobody
    # is waiting on.
    for _ in range(n_samples):
        category: FailureCategory = _draw_from_mix(rng, CATEGORY_FREQUENCY)  # type: ignore[assignment]
        method_mix = _METHOD_MIX_OVERRIDES.get(category, _DEFAULT_METHOD_MIX)
        method: PaymentMethod = _draw_from_mix(rng, method_mix)  # type: ignore[assignment]
        strategy = _draw_strategy(rng, category)

        attempt_number: int = _draw_from_mix(rng, _ATTEMPT_DISTRIBUTION)  # type: ignore[assignment]
        amount_rupees = _draw_amount_rupees(rng)
        total_payments, observed_rate, latent_quality = _draw_customer(rng)

        hours_since_failure = _draw_hours_since_failure(rng)

        probability = success_probability(
            failure_category=category,
            proposed_strategy=strategy,
            amount_rupees=amount_rupees,
            attempt_number=attempt_number,
            customer_quality=latent_quality,
            hours_since_failure=hours_since_failure,
        )

        # The unobservable residual: issuer policy that morning, whether the
        # customer's phone had signal, whether their spouse said no. Nothing in
        # the feature row can explain it, so it sets a hard ceiling on any
        # achievable ROC-AUC -- which is the honest situation for this problem.
        probability *= float(rng.lognormal(mean=0.0, sigma=LATENT_NOISE_SIGMA))
        probability = min(MAX_PROBABILITY, max(MIN_PROBABILITY, probability))

        row = build_feature_row(
            failure_category=category,
            payment_method=method,
            proposed_strategy=strategy,
            # The generator works in rupees, so convert back to the paise that
            # build_feature_row expects. Rounding to whole rupees first keeps the
            # value integral in paise, matching how amounts are actually stored.
            amount_paise=int(round(amount_rupees)) * 100,
            attempt_number=attempt_number,
            customer_prior_success_rate=observed_rate,
            customer_total_payments=total_payments,
            hours_since_failure=hours_since_failure,
        )
        row[TARGET] = int(rng.random() < probability)
        rows.append(row)

    return pd.DataFrame(rows, columns=[*FEATURE_COLUMNS, TARGET])
