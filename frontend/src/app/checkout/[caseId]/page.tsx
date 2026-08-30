"use client";

/**
 * `/checkout/[caseId]` - the customer-facing recovery page.
 *
 * This is the one screen in the product that is not written for the merchant.
 * The person looking at it does not know what a guardrail is, has never heard of
 * a propensity score, and does not care which model classified their failure.
 * They want to know four things: who is asking, what for, how much, and what to
 * press. Everything else on this page is subtracted until only those four
 * remain - one centred column, one amount, one primary action.
 *
 * Consequences of that, made explicit because they look like omissions:
 *
 *  - No merchant vocabulary. The case status is translated into plain sentences
 *    ("this link has expired"), never rendered as `RECOVERY_STATUS_LABEL`.
 *  - No agent output. The rationale, the classification and the score belong to
 *    the operator's decision screen; showing a customer that a model scored
 *    their likelihood of paying would be both useless and off-putting.
 *  - No console chrome of our own. The application shell (header, nav, theme
 *    control) is applied by the root layout for every route; this page adds no
 *    back links, no tables and no secondary navigation on top of it.
 *
 * Data flow. The screen reads the *case* first, because the case's status is
 * what decides whether there is anything to pay at all, and then reads the
 * checkout session only when the case is actually payable. Asking for a checkout
 * session on a case that was already recovered would surface as an error the
 * customer cannot act on, when the truthful answer - "this is already paid" - is
 * sitting one field away in the case.
 */

import { use, useCallback, useEffect, useState } from "react";
import { Check, Clock, FlaskConical, Lock, RefreshCw } from "lucide-react";

import { RazorpayCheckout } from "@/components/recovery/razorpay-checkout";
import { Alert, AlertDescription, AlertTitle, type AlertVariant } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { usePolling } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { formatDateTime, formatRupees, truncateId } from "@/lib/format";
import type { CheckoutSession, RecoveryCase, RecoveryStatus } from "@/lib/types";
import { errorMessage } from "@/lib/utils";

/**
 * The wire contract carries no merchant identity: RecoverAI is a single-merchant
 * demonstration, so `CheckoutSessionOut` describes the customer and the amount
 * but never who is collecting. Rather than invent a name in three components,
 * it is stated once here and passed down.
 */
const MERCHANT_NAME = "RecoverAI";

/** The one case state in which a payment link is live and payable. */
const PAYABLE_STATUS: RecoveryStatus = "awaiting_payment";

/**
 * How often to re-read the case while the link is live.
 *
 * The state that matters here changes *off-screen*: the customer may complete
 * the payment in the Razorpay window, in another tab, or the merchant may mark
 * the attempt failed, and none of that causes a render in this tab. With no
 * websocket in the stack, polling is the honest way to notice. Five seconds is
 * slow enough to be invisible on the server and fast enough that the page does
 * not sit lying about the state for long.
 */
const POLL_INTERVAL_MS = 5_000;

/** What the page loads in one pass: always the case, the session only if payable. */
interface CheckoutView {
  recoveryCase: RecoveryCase;
  /** `null` when the case is not in a payable state - there is no live order. */
  session: CheckoutSession | null;
}

/**
 * Plain-language explanation for every case state that cannot be paid.
 *
 * Typed as an exhaustive `Record` over the non-payable, non-recovered states, so
 * adding a status to the backend enum and mirroring it in `@/lib/types` fails to
 * compile here until someone has written the sentence a customer should read.
 * The alternative - a `default:` branch saying "this link is unavailable" - is
 * how a customer ends up staring at a dead page with no idea whether they have
 * been charged, which is the single worst outcome this screen can produce.
 *
 * Every message answers the money question explicitly. "Nothing has been
 * charged" is the sentence the person is actually looking for.
 */
const UNPAYABLE_EXPLANATION: Record<
  Exclude<RecoveryStatus, "recovered" | "awaiting_payment">,
  { title: string; body: string; variant: AlertVariant }
> = {
  proposed: {
    title: "This payment is still being reviewed",
    body: `${MERCHANT_NAME} is reviewing the failed payment. If a retry is offered you will be sent a fresh link. Nothing has been charged.`,
    variant: "neutral",
  },
  awaiting_approval: {
    title: "This payment is still being reviewed",
    body: `Someone at ${MERCHANT_NAME} is deciding whether to retry this payment. If they go ahead you will be sent a link. Nothing has been charged.`,
    variant: "neutral",
  },
  approved: {
    title: "Your payment link is being prepared",
    body: "The retry has been approved and the order is being created. This page will update on its own in a few seconds.",
    variant: "info",
  },
  executing: {
    title: "Your payment link is being prepared",
    body: "The order is being created right now. This page will update on its own in a few seconds.",
    variant: "info",
  },
  blocked: {
    title: "This payment link is not available",
    body: `An automated safety check stopped this retry before it was offered. Nothing has been charged. Contact ${MERCHANT_NAME} if you would still like to pay.`,
    variant: "neutral",
  },
  rejected: {
    title: "This payment link was withdrawn",
    body: `${MERCHANT_NAME} decided not to retry this payment. Nothing has been charged.`,
    variant: "neutral",
  },
  failed: {
    title: "This payment did not go through",
    body: `The attempt was not completed and this link is now closed. Nothing has been charged. Ask ${MERCHANT_NAME} for a new link if you would still like to pay.`,
    variant: "danger",
  },
  expired: {
    title: "This payment link has expired",
    body: `Recovery links stay valid for a short window and this one has passed it. Nothing has been charged - ask ${MERCHANT_NAME} to send a new one.`,
    variant: "warning",
  },
  no_action: {
    title: "There is nothing to pay here",
    body: "No retry was needed for this payment. Nothing has been charged.",
    variant: "neutral",
  },
  escalated: {
    title: "Someone is looking at this personally",
    body: `${MERCHANT_NAME} has taken this over manually and will be in touch. Nothing has been charged.`,
    variant: "neutral",
  },
};

/**
 * Narrows a case status to a key of the explanation map.
 *
 * Written as a positive check rather than a cast: `recovered` and
 * `awaiting_payment` are handled by their own branches before this is reached,
 * and the compiler is told that rather than being told to trust us.
 */
function unpayableKey(
  status: RecoveryStatus,
): Exclude<RecoveryStatus, "recovered" | "awaiting_payment"> | null {
  if (status === "recovered" || status === "awaiting_payment") return null;
  return status;
}

export default function CheckoutPage({ params }: { params: Promise<{ caseId: string }> }) {
  // Route params are a Promise in Next 15. `use()` unwraps it inside the client
  // component; there is no server component above this one to await it.
  const { caseId } = use(params);

  /**
   * The case as returned by the action the customer just completed.
   *
   * `verifyPayment` and `simulateCheckout` both answer with the authoritative
   * post-payment case, so the confirmation can render immediately instead of
   * waiting up to five seconds for the next poll to agree. It also takes
   * precedence over the polled copy, which may still be one cycle stale.
   */
  const [settled, setSettled] = useState<RecoveryCase | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingSimulation, setPendingSimulation] = useState<"success" | "failure" | null>(null);

  const load = useCallback(async (): Promise<CheckoutView> => {
    const recoveryCase = await api.getCase(caseId);
    // Only a live link has a session behind it. Skipping the second request on a
    // settled case is not an optimisation - it is what stops the page rendering
    // a gateway error over the top of "you have already paid this".
    if (recoveryCase.status !== PAYABLE_STATUS) return { recoveryCase, session: null };
    return { recoveryCase, session: await api.getCheckoutSession(caseId) };
  }, [caseId]);

  const [pollIntervalMs, setPollIntervalMs] = useState(POLL_INTERVAL_MS);
  const { data, error, loading, refresh } = usePolling(load, [caseId], pollIntervalMs);

  const recoveryCase = settled ?? data?.recoveryCase ?? null;

  /**
   * Stand the poll down once the case can no longer change.
   *
   * The interval has to be state rather than a value derived inline, because it
   * is an argument to the hook that produces the data it depends on. Polling a
   * terminal case forever would be a request every five seconds, for as long as
   * a tab is left open on a page that will never change again.
   */
  useEffect(() => {
    if (recoveryCase === null) return;
    setPollIntervalMs(recoveryCase.status === PAYABLE_STATUS ? POLL_INTERVAL_MS : 0);
  }, [recoveryCase]);

  const runSimulation = async (succeed: boolean): Promise<void> => {
    setPendingSimulation(succeed ? "success" : "failure");
    setActionError(null);
    try {
      setSettled(await api.simulateCheckout(caseId, succeed));
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setPendingSimulation(null);
    }
  };

  return (
    <div className="mx-auto flex w-full max-w-md flex-col gap-5 py-4 sm:py-10">
      <PageHeading />

      {/* A failure to reach the API is shown above whatever is already on screen
          rather than replacing it: a customer mid-payment should not lose the
          amount they were looking at because one poll blipped. */}
      {error !== null && recoveryCase !== null ? (
        <Alert variant="warning">
          <AlertTitle>This page could not refresh</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      ) : null}

      {recoveryCase === null ? (
        loading ? (
          <LoadingCard />
        ) : (
          <UnreachableCard message={error ?? "This payment link could not be loaded."} onRetry={refresh} />
        )
      ) : recoveryCase.status === "recovered" ? (
        <RecoveredCard recoveryCase={recoveryCase} />
      ) : (
        <PayableOrExplained
          recoveryCase={recoveryCase}
          session={data?.session ?? null}
          actionError={actionError}
          pendingSimulation={pendingSimulation}
          onSimulate={runSimulation}
          onSettled={setSettled}
          onRetry={refresh}
        />
      )}

      <Footnote />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Sections                                                                    */
/* -------------------------------------------------------------------------- */

function PageHeading() {
  return (
    <div className="flex flex-col items-center gap-1 text-center">
      <p className="text-xs font-medium uppercase tracking-widest text-slate-400 dark:text-slate-500">
        Secure payment
      </p>
      <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">{MERCHANT_NAME}</h1>
    </div>
  );
}

function LoadingCard() {
  return (
    <Card aria-busy="true">
      <CardContent className="space-y-4 pt-5">
        <Skeleton className="h-3 w-24" />
        <Skeleton className="h-9 w-40" />
        <Skeleton className="h-3 w-56" />
        <Skeleton className="h-11 w-full" />
      </CardContent>
    </Card>
  );
}

/**
 * Shown when the case itself could not be read - the backend is down, or the id
 * in the URL does not exist. The retry button matters: a customer who followed a
 * link from an email has no other way back to this page.
 */
function UnreachableCard({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <Card>
      <CardContent className="space-y-4 pt-5">
        <Alert variant="danger">
          <AlertTitle>This payment link could not be opened</AlertTitle>
          <AlertDescription>{message}</AlertDescription>
        </Alert>
        <Button variant="outline" className="w-full" leadingIcon={<RefreshCw className="h-4 w-4" />} onClick={onRetry}>
          Try again
        </Button>
      </CardContent>
    </Card>
  );
}

/**
 * The payoff.
 *
 * This is the moment the entire product exists to produce: a payment that had
 * already failed has now actually been collected. It is worth letting that land.
 * The warmth here is deliberate but stays inside the calm design language - a
 * single emerald tick, the amount set large, and one sentence closing the loop
 * ("the merchant has been notified") so the customer knows they are done and
 * nothing further is expected of them. No confetti, no animation, no exclamation
 * marks: this is still a receipt, and a receipt that shouts is not reassuring.
 */
function RecoveredCard({ recoveryCase }: { recoveryCase: RecoveryCase }) {
  // The recovered amount is the value of record for this screen. It falls back
  // to the case amount only because a case that reached `recovered` before the
  // amount column was written would otherwise render a confident "₹0.00", which
  // is worse than being approximately right.
  const paise =
    recoveryCase.recovered_amount_paise > 0
      ? recoveryCase.recovered_amount_paise
      : recoveryCase.amount_paise;

  return (
    <Card className="border-emerald-200 dark:border-emerald-900">
      <CardContent className="flex flex-col items-center gap-4 px-6 py-10 text-center">
        <span
          aria-hidden="true"
          className="flex h-12 w-12 items-center justify-center rounded-full bg-emerald-50 text-emerald-600 dark:bg-emerald-950 dark:text-emerald-400"
        >
          <Check className="h-6 w-6" strokeWidth={2.5} />
        </span>

        <div className="space-y-1">
          <p className="text-sm font-medium text-emerald-700 dark:text-emerald-400">Payment received</p>
          <p className="font-mono text-3xl font-semibold tabular-nums tracking-tight text-slate-900 dark:text-slate-50">
            {formatRupees(paise)}
          </p>
        </div>

        <p className="max-w-xs text-sm leading-relaxed text-slate-600 dark:text-slate-400">
          Thank you - that payment has gone through. {MERCHANT_NAME} has been notified and there is
          nothing further for you to do.
        </p>

        <Separator className="my-1" />

        <dl className="grid w-full grid-cols-2 gap-y-2 text-left text-xs">
          <dt className="text-slate-500 dark:text-slate-400">Reference</dt>
          <dd className="text-right font-mono text-slate-700 dark:text-slate-300">
            {truncateId(recoveryCase.id, 8, 6)}
          </dd>
          {recoveryCase.recovered_at !== null ? (
            <>
              <dt className="text-slate-500 dark:text-slate-400">Paid at</dt>
              <dd className="text-right text-slate-700 dark:text-slate-300">
                {formatDateTime(recoveryCase.recovered_at)}
              </dd>
            </>
          ) : null}
        </dl>
      </CardContent>
    </Card>
  );
}

interface PayableOrExplainedProps {
  recoveryCase: RecoveryCase;
  session: CheckoutSession | null;
  actionError: string | null;
  pendingSimulation: "success" | "failure" | null;
  onSimulate: (succeed: boolean) => void;
  onSettled: (updated: RecoveryCase) => void;
  onRetry: () => void;
}

function PayableOrExplained({
  recoveryCase,
  session,
  actionError,
  pendingSimulation,
  onSimulate,
  onSettled,
  onRetry,
}: PayableOrExplainedProps) {
  const explanationKey = unpayableKey(recoveryCase.status);

  if (explanationKey !== null) {
    const explanation = UNPAYABLE_EXPLANATION[explanationKey];

    // Four of the non-payable states are on their way *towards* a link rather
    // than past one, and the amount is genuinely still owed in all of them. Only
    // the closed states strike it through - showing "still being prepared" next
    // to a struck-out amount would say the opposite of what it means.
    const stillDue =
      recoveryCase.status === "proposed" ||
      recoveryCase.status === "awaiting_approval" ||
      recoveryCase.status === "approved" ||
      recoveryCase.status === "executing";

    return (
      <Card>
        <CardContent className="space-y-4 pt-5">
          <Alert variant={explanation.variant}>
            <AlertTitle>{explanation.title}</AlertTitle>
            <AlertDescription>{explanation.body}</AlertDescription>
          </Alert>

          <AmountSummary
            // "Amount due" on a link that is closed would be an instruction the
            // customer cannot act on, so the closed states relabel it as history.
            label={stillDue ? "Amount due" : "Original amount"}
            paise={recoveryCase.amount_paise}
            description={recoveryCase.original_payment?.description ?? ""}
            muted={!stillDue}
          />

          <Button
            variant="ghost"
            size="sm"
            className="w-full"
            leadingIcon={<RefreshCw className="h-3.5 w-3.5" />}
            onClick={onRetry}
          >
            Check again
          </Button>
        </CardContent>
      </Card>
    );
  }

  // Payable, but the session request has not landed yet (or failed on its own).
  // Rendering a pay button with no order behind it would be a button that cannot
  // work, so the panel waits instead.
  if (session === null) {
    return <LoadingCard />;
  }

  return (
    <Card>
      <CardContent className="space-y-5 pt-5">
        <AmountSummary paise={session.amount_paise} description={session.description} />

        <Separator />

        {actionError !== null ? (
          <Alert variant="danger">
            <AlertTitle>That did not complete</AlertTitle>
            <AlertDescription>{actionError}</AlertDescription>
          </Alert>
        ) : null}

        {session.gateway_mode === "razorpay_test" ? (
          <RazorpayCheckout session={session} merchantName={MERCHANT_NAME} onSettled={onSettled} />
        ) : (
          <SimulatedPanel
            session={session}
            pending={pendingSimulation}
            onSimulate={onSimulate}
          />
        )}

        {session.expires_at !== null ? (
          <p className="flex items-center justify-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
            <Clock className="h-3.5 w-3.5" aria-hidden="true" />
            {/* Absolute rather than relative ("in 42m"): relative time reads the
                wall clock, which differs between the server render and hydration
                and would flag a mismatch on a screen shown to a customer. */}
            This link is valid until {formatDateTime(session.expires_at)}
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

function AmountSummary({
  paise,
  description,
  label = "Amount due",
  muted = false,
}: {
  paise: number;
  description: string;
  label?: string;
  muted?: boolean;
}) {
  return (
    <div className="space-y-1 text-center">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {label}
      </p>
      <p
        className={
          muted
            ? "font-mono text-2xl font-semibold tabular-nums tracking-tight text-slate-400 line-through dark:text-slate-600"
            : "font-mono text-3xl font-semibold tabular-nums tracking-tight text-slate-900 dark:text-slate-50"
        }
      >
        {formatRupees(paise)}
      </p>
      {description !== "" ? (
        <p className="text-sm text-slate-600 dark:text-slate-400">{description}</p>
      ) : null}
    </div>
  );
}

/**
 * The simulated gateway's checkout.
 *
 * Two labelled buttons, and a banner that says in plain words that no real
 * payment network is involved. It would have been easy to draw a convincing
 * card form here and have it "succeed" - and that would misrepresent the demo.
 * A reviewer looking at this screen has to be able to tell, without reading the
 * source, that they are looking at a simulator; anything less makes every other
 * honest claim in the product less believable.
 *
 * The failure button is not decoration either. A recovery product whose demo can
 * only ever show the happy path has not demonstrated recovery; being able to
 * drive the case into `failed` on demand is what makes the failure handling
 * inspectable.
 */
function SimulatedPanel({
  session,
  pending,
  onSimulate,
}: {
  session: CheckoutSession;
  pending: "success" | "failure" | null;
  onSimulate: (succeed: boolean) => void;
}) {
  const amount = formatRupees(session.amount_paise);

  return (
    <div className="space-y-3">
      <Alert variant="warning" icon={<FlaskConical className="h-4 w-4" aria-hidden="true" />}>
        <AlertTitle>Simulated payment</AlertTitle>
        <AlertDescription>
          No Razorpay credentials are configured, so this checkout is an in-process simulator.
          Nothing here touches a real payment network and no money moves. The signature it produces
          is a genuine HMAC and is still verified server-side.
        </AlertDescription>
      </Alert>

      <Button
        variant="success"
        size="lg"
        className="w-full"
        loading={pending === "success"}
        loadingText="Completing payment…"
        disabled={pending !== null}
        onClick={() => onSimulate(true)}
      >
        Pay {amount} (simulated success)
      </Button>

      <Button
        variant="outline"
        className="w-full"
        loading={pending === "failure"}
        loadingText="Recording failure…"
        disabled={pending !== null}
        onClick={() => onSimulate(false)}
      >
        Simulate a failed payment
      </Button>
    </div>
  );
}

function Footnote() {
  return (
    <p className="flex items-center justify-center gap-1.5 text-center text-xs text-slate-400 dark:text-slate-500">
      <Lock className="h-3 w-3 shrink-0" aria-hidden="true" />
      Card details are never entered on this page or stored by {MERCHANT_NAME}.
    </p>
  );
}
