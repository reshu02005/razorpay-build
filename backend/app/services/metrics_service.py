"""
Read-only aggregation for the merchant dashboard.

Every number here is computed by the database with ``SUM`` and ``COUNT``, never
by loading rows into Python and adding them up in a loop. That is not
micro-optimisation: a merchant with a year of payments has hundreds of thousands
of rows, and a dashboard that pulls all of them to compute five totals stops
rendering long before the data stops being interesting. Aggregating in SQL keeps
the work proportional to the *answer* rather than to the history.

**Which population each figure is measured over.** Volume, counts and the failure
rate are measured over *original* payments only -- rows with
``is_recovery_attempt = False``. A recovery attempt is stored in the same table
by design, so including it would count one customer's single intention to pay
twice, and would dilute the failure rate with rows that only exist because
something already failed. Recovered volume is read from the case table instead,
so that "recovered" and "recoverable" are measured over the same population; a
ratio between two different populations is not a rate, it is a coincidence.
"""

from __future__ import annotations

import logging

from sqlalchemy import case as sql_case
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Payment, RecoveryAttempt, RecoveryCase, utcnow
from app.domain.enums import (
    FailureCategory,
    GuardrailDecision,
    PaymentStatus,
    RecoveryStatus,
    RecoveryStrategy,
)
from app.domain.schemas import DashboardMetricsOut, FailureBreakdownItem, paise_to_rupees
from app.services.payment_service import utc_day_start

logger = logging.getLogger(__name__)


class MetricsService:
    """
    Aggregates the dashboard's figures. Reads only; never commits.

    Takes only a session, matching the other services. The policy limit it needs
    -- the daily recovery budget -- is read from the settings singleton rather
    than passed in, because a limit is not a per-request input: if the dashboard
    could be handed a different budget than the guardrail engine enforces, the
    "budget used" gauge would be measuring against a number nobody is bound by.
    """

    def __init__(self, db: Session) -> None:
        """
        Args:
            db: The request-scoped session.
        """
        self.db = db

    def dashboard(self) -> DashboardMetricsOut:
        """
        Compute every headline figure for the merchant dashboard.

        Returns:
            ``DashboardMetricsOut`` with volumes in both paise and rupees, case
            counts by state, the recovery rate and today's budget usage.
        """
        settings = get_settings()

        # One grouped query for all five payment figures. The alternative -- a
        # scalar query per status -- would be five round trips to compute what a
        # single GROUP BY already produces.
        by_status: dict[str, tuple[int, int]] = {
            status: (count, volume)
            for status, count, volume in self.db.execute(
                select(
                    Payment.status,
                    func.count(Payment.id),
                    func.coalesce(func.sum(Payment.amount_paise), 0),
                )
                .where(Payment.is_recovery_attempt.is_(False))
                .group_by(Payment.status)
            ).all()
        }

        total_payments = sum(count for count, _ in by_status.values())
        total_volume_paise = sum(volume for _, volume in by_status.values())
        failed_payments, failed_volume_paise = by_status.get(PaymentStatus.FAILED.value, (0, 0))
        _, captured_volume_paise = by_status.get(PaymentStatus.CAPTURED.value, (0, 0))

        # Failed payments nobody has analysed yet: the merchant's to-do list.
        # A LEFT JOIN with a NULL test rather than a NOT IN subquery, because the
        # join uses the index on recovery_cases.original_payment_id.
        unanalysed_failures = self.db.execute(
            select(func.count(Payment.id))
            .select_from(Payment)
            .outerjoin(RecoveryCase, RecoveryCase.original_payment_id == Payment.id)
            .where(
                Payment.status == PaymentStatus.FAILED.value,
                Payment.is_recovery_attempt.is_(False),
                RecoveryCase.id.is_(None),
            )
        ).scalar_one()

        # Failed volume that could actually have been collected. This is the
        # honest denominator for a recovery rate: money policy would never have
        # let us chase is not money we failed to recover.
        #
        # Both predicates are needed, and the second is easy to miss. Filtering
        # only on `guardrail_decision != DENY` looks right but silently includes
        # every NO_ACTION and ESCALATED case, because the policy engine
        # short-circuits to ALLOW for any strategy that moves no money -- the
        # rules did not permit those recoveries, they were never asked. That put
        # fraud-blocked payments into the denominator of the headline metric,
        # directly contradicting R3, whose whole purpose is "this category is
        # never recoverable".
        attempting_strategies = [s.value for s in RecoveryStrategy if s.moves_money]
        recoverable_volume_paise = self.db.execute(
            select(func.coalesce(func.sum(RecoveryCase.amount_paise), 0)).where(
                RecoveryCase.guardrail_decision != GuardrailDecision.DENY.value,
                RecoveryCase.strategy.in_(attempting_strategies),
            )
        ).scalar_one()

        recovered_volume_paise = self.db.execute(
            select(func.coalesce(func.sum(RecoveryCase.recovered_amount_paise), 0)).where(
                RecoveryCase.status == RecoveryStatus.RECOVERED.value
            )
        ).scalar_one()

        cases_by_status: dict[str, int] = {
            status: count
            for status, count in self.db.execute(
                select(RecoveryCase.status, func.count(RecoveryCase.id)).group_by(RecoveryCase.status)
            ).all()
        }

        daily_budget_used_paise = self.db.execute(
            select(func.coalesce(func.sum(RecoveryAttempt.amount_paise), 0)).where(
                RecoveryAttempt.created_at >= utc_day_start(utcnow())
            )
        ).scalar_one()

        return DashboardMetricsOut(
            total_volume_paise=total_volume_paise,
            captured_volume_paise=captured_volume_paise,
            failed_volume_paise=failed_volume_paise,
            recoverable_volume_paise=recoverable_volume_paise,
            recovered_volume_paise=recovered_volume_paise,
            total_volume_rupees=paise_to_rupees(total_volume_paise),
            captured_volume_rupees=paise_to_rupees(captured_volume_paise),
            failed_volume_rupees=paise_to_rupees(failed_volume_paise),
            recoverable_volume_rupees=paise_to_rupees(recoverable_volume_paise),
            recovered_volume_rupees=paise_to_rupees(recovered_volume_paise),
            total_payments=total_payments,
            failed_payments=failed_payments,
            unanalysed_failures=unanalysed_failures,
            cases_total=sum(cases_by_status.values()),
            cases_awaiting_approval=cases_by_status.get(RecoveryStatus.AWAITING_APPROVAL.value, 0),
            cases_blocked=cases_by_status.get(RecoveryStatus.BLOCKED.value, 0),
            cases_recovered=cases_by_status.get(RecoveryStatus.RECOVERED.value, 0),
            cases_failed=cases_by_status.get(RecoveryStatus.FAILED.value, 0),
            recovery_rate_pct=_ratio_pct(recovered_volume_paise, recoverable_volume_paise),
            failure_rate_pct=_ratio_pct(failed_payments, total_payments),
            daily_budget_used_paise=daily_budget_used_paise,
            daily_budget_limit_paise=settings.daily_recovery_budget_paise,
        )

    def failure_breakdown(self) -> list[FailureBreakdownItem]:
        """
        Count and value failures by category, busiest first.

        Returns:
            One item per failure category that has at least one case.

        Measured over recovery *cases* rather than payments, because a category
        only exists once a payment has been classified. Failures nobody has
        analysed yet are therefore absent here on purpose -- they are reported
        separately as ``unanalysed_failures`` on the dashboard, where "we have not
        looked at these" reads as the call to action it is, rather than hiding
        inside an "unknown" slice that would be indistinguishable from failures
        the classifier genuinely could not place.
        """
        rows = self.db.execute(
            select(
                RecoveryCase.failure_category,
                func.count(RecoveryCase.id),
                func.coalesce(func.sum(RecoveryCase.amount_paise), 0),
                # Recovered cases counted in the same pass. A second query
                # filtered on status would have to be joined back by category in
                # Python, which is the loop this module exists to avoid.
                func.coalesce(
                    func.sum(
                        sql_case(
                            (RecoveryCase.status == RecoveryStatus.RECOVERED.value, 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .group_by(RecoveryCase.failure_category)
            .order_by(func.count(RecoveryCase.id).desc())
        ).all()

        return [
            FailureBreakdownItem(
                category=FailureCategory(category),
                count=count,
                volume_paise=volume_paise,
                volume_rupees=paise_to_rupees(volume_paise),
                recovered_count=recovered_count,
            )
            for category, count, volume_paise, recovered_count in rows
        ]


def _ratio_pct(numerator: int, denominator: int) -> float:
    """
    Express one figure as a percentage of another, safely.

    Args:
        numerator: The part.
        denominator: The whole.

    Returns:
        The percentage, rounded to two decimals, or ``0.0`` when the denominator
        is zero.

    A freshly installed database has nothing in it, and the first thing a
    reviewer does is open the dashboard. "0%" is the correct reading of "nothing
    recovered out of nothing recoverable"; a ``ZeroDivisionError`` would render
    that as a 500, and a blank would leave the reader unable to tell an empty
    system from a broken one. Reported, not hidden.
    """
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator * 100, 2)
