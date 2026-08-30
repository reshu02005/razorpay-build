'use client';

/**
 * /payments/[paymentId] - one payment, and the single decision available on it.
 *
 * This screen is deliberately narrow in scope. It shows what the gateway
 * recorded, who the customer is, and offers exactly one action: hand the
 * failure to the agent. Everything about *what should be done* lives on the
 * recovery screen, because the moment a payment screen starts recommending
 * things it becomes a second, weaker version of the decision console.
 *
 * The analysis button is the interesting part of the page. Behind it, the
 * server classifies the failure, scores it with the propensity model, runs a
 * function-calling loop against Gemini (or the deterministic planner when no
 * key is configured) and evaluates thirteen guardrails. On the LLM path that
 * routinely takes several seconds. A button that simply freezes for five
 * seconds reads as a bug, and the operator's next move is to click it again -
 * so the wait is narrated, and the control is disabled while the request is in
 * flight.
 */

import { use, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ArrowLeft, ArrowRight, Mail, Phone, ShieldAlert, Sparkles, User } from 'lucide-react';

import { FailureDetail } from '@/components/recovery/failure-detail';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button, buttonVariants } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { Spinner } from '@/components/ui/spinner';
import { useApi } from '@/hooks/useApi';
import { ApiRequestError, api } from '@/lib/api';
import { formatConfidence, formatDateTime, formatRupees } from '@/lib/format';
import {
  PAYMENT_STATUS_LABEL,
  PAYMENT_STATUS_TONE,
  type Customer,
} from '@/lib/types';
import { cn, errorMessage } from '@/lib/utils';

function CustomerCard({ customer }: { customer: Customer }) {
  return (
    <Card>
      <CardHeader
        action={
          customer.risk_flagged ? (
            <Badge variant="danger" icon={<ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />}>
              Risk flagged
            </Badge>
          ) : null
        }
      >
        <CardTitle className="flex items-center gap-2">
          <User className="h-4 w-4 text-slate-500 dark:text-slate-400" aria-hidden="true" />
          Customer
        </CardTitle>
        <CardDescription>
          Their history is an input to the propensity model, not a judgement about them.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        <div>
          <p className="text-sm font-medium text-slate-900 dark:text-slate-100">{customer.name}</p>
          <p className="mt-1 flex items-center gap-1.5 truncate text-xs text-slate-500 dark:text-slate-400">
            <Mail className="h-3 w-3 shrink-0" aria-hidden="true" />
            {customer.email}
          </p>
          {customer.phone ? (
            <p className="mt-0.5 flex items-center gap-1.5 text-xs text-slate-500 dark:text-slate-400">
              <Phone className="h-3 w-3 shrink-0" aria-hidden="true" />
              {customer.phone}
            </p>
          ) : null}
        </div>

        <Separator />

        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
          <div>
            <dt className="text-2xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Prior success rate
            </dt>
            <dd className="mt-0.5 tabular-nums text-slate-900 dark:text-slate-100">
              {formatConfidence(customer.prior_success_rate)}
            </dd>
          </div>
          <div>
            <dt className="text-2xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Payments
            </dt>
            <dd className="mt-0.5 tabular-nums text-slate-900 dark:text-slate-100">
              {customer.successful_payments} of {customer.total_payments} succeeded
            </dd>
          </div>
          <div className="col-span-2">
            <dt className="text-2xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Lifetime value
            </dt>
            <dd className="mt-0.5 tabular-nums text-slate-900 dark:text-slate-100">
              {formatRupees(customer.lifetime_value_paise)}
            </dd>
          </div>
        </dl>

        {customer.risk_flagged ? (
          <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs leading-relaxed text-rose-900 dark:border-rose-900 dark:bg-rose-950/60 dark:text-rose-200">
            {/* Stated here rather than left for the operator to discover after
                running an analysis: rule R12 denies outright on a risk flag, so
                the outcome of pressing Analyse is already known. */}
            This customer is flagged for risk. A guardrail denies automated recovery for flagged
            customers, so any case opened here will be blocked.
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

export default function PaymentDetailPage({
  params,
}: {
  params: Promise<{ paymentId: string }>;
}) {
  /*
   * Next 15 hands route params to a client page as a Promise. `React.use()`
   * unwraps it; destructuring `params` directly - the Next 14 shape that every
   * older tutorial shows - compiles cleanly and then renders `undefined` into
   * the request URL, which is a genuinely confusing way to spend an afternoon.
   */
  const { paymentId } = use(params);

  const router = useRouter();
  const { data: payment, error, loading, refresh } = useApi(
    () => api.getPayment(paymentId),
    [paymentId],
  );

  const [analysing, setAnalysing] = useState(false);
  const [analyseError, setAnalyseError] = useState<string | null>(null);

  const handleAnalyse = async () => {
    setAnalyseError(null);
    setAnalysing(true);
    try {
      const recoveryCase = await api.analyzePayment(paymentId, {});
      router.push(`/recovery/${recoveryCase.id}`);
      // Navigation is in flight; the button stays disabled until this page
      // unmounts so a second click cannot start a second analysis run.
      return;
    } catch (err: unknown) {
      /*
       * A 409 means a case already exists for this payment. That is not really
       * an error from the operator's point of view - they wanted to get to the
       * case, and there is one. Refetching the payment repopulates
       * `recovery_case_id`, which turns the button below into "View recovery
       * case" without them having to work out what happened.
       */
      if (err instanceof ApiRequestError && err.code === 'duplicate_case') {
        refresh();
      }
      setAnalyseError(errorMessage(err));
      setAnalysing(false);
    }
  };

  if (payment === null) {
    return (
      <div className="space-y-6">
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-sm text-slate-500 transition-colors hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          Dashboard
        </Link>

        {loading ? (
          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
            <Skeleton className="h-96 w-full" />
            <div className="space-y-6">
              <Skeleton className="h-40 w-full" />
              <Skeleton className="h-64 w-full" />
            </div>
          </div>
        ) : (
          <Alert variant="danger">
            <AlertTitle>This payment could not be loaded</AlertTitle>
            <AlertDescription>{error ?? `No payment was returned for id ${paymentId}.`}</AlertDescription>
          </Alert>
        )}
      </div>
    );
  }

  const hasCase = payment.recovery_case_id !== null;
  const isFailed = payment.status === 'failed';

  return (
    <div className="space-y-8">
      <header className="space-y-4">
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-sm text-slate-500 transition-colors hover:text-slate-900 dark:text-slate-400 dark:hover:text-slate-100"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          Dashboard
        </Link>

        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="font-display text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
              Payment
            </h1>
            <p className="mt-1 font-mono text-xs text-slate-500 dark:text-slate-400">
              {payment.id}
            </p>
          </div>

          <div className="flex items-center gap-3">
            <span className="font-display text-xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
              {formatRupees(payment.amount_paise)}
            </span>
            <Badge variant={PAYMENT_STATUS_TONE[payment.status]} dot>
              {PAYMENT_STATUS_LABEL[payment.status]}
            </Badge>
          </div>
        </div>

        <p className="text-xs text-slate-500 dark:text-slate-400">
          Recorded {formatDateTime(payment.created_at)}
        </p>
      </header>

      {error === null ? null : (
        <Alert variant="warning">
          <AlertTitle>The last refresh failed</AlertTitle>
          <AlertDescription>{error} Everything below is the last successful read.</AlertDescription>
        </Alert>
      )}

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
        {/* `customer` is not passed here: this page gives the customer a card of
            their own below, and repeating the same three fields twice on one
            screen is noise, not reinforcement. */}
        <FailureDetail
          payment={payment}
          title="Payment record"
          description="The gateway's own account of this payment, unedited."
        />

        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-sky-600 dark:text-sky-400" aria-hidden="true" />
                Recovery
              </CardTitle>
              <CardDescription>
                {hasCase
                  ? 'A recovery case has already been opened for this payment.'
                  : isFailed
                    ? 'Classify the failure, score it, and propose a strategy inside the guardrails.'
                    : 'Recovery applies to failed payments only.'}
              </CardDescription>
            </CardHeader>

            <CardContent className="space-y-3">
              {analyseError === null ? null : (
                <Alert variant="danger">
                  <AlertDescription>{analyseError}</AlertDescription>
                </Alert>
              )}

              {hasCase && payment.recovery_case_id !== null ? (
                <Link
                  href={`/recovery/${payment.recovery_case_id}`}
                  className={cn(buttonVariants({ variant: 'default' }), 'w-full')}
                >
                  View recovery case
                  <ArrowRight className="h-4 w-4" aria-hidden="true" />
                </Link>
              ) : isFailed ? (
                <>
                  <Button
                    className="w-full"
                    loading={analysing}
                    loadingText="Analysing…"
                    onClick={() => {
                      void handleAnalyse();
                    }}
                    leadingIcon={<Sparkles className="h-4 w-4" aria-hidden="true" />}
                  >
                    Analyse with RecoverAI
                  </Button>

                  {analysing ? (
                    // Naming the four things happening server-side turns a wait
                    // into progress. The alternative - a silent five-second
                    // freeze - is indistinguishable from a hung request.
                    <p className="flex items-start gap-2 text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                      <Spinner size="xs" className="mt-0.5 shrink-0" label="Analysing" />
                      Classifying the failure, scoring recovery propensity, and evaluating the
                      guardrails. The model-backed path can take a few seconds.
                    </p>
                  ) : (
                    <p className="text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                      Opens a case for review. Nothing is charged, and no order is created, until a
                      human approves it.
                    </p>
                  )}
                </>
              ) : (
                <p className="text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                  This payment is {PAYMENT_STATUS_LABEL[payment.status].toLowerCase()}, so there is
                  nothing to recover.
                </p>
              )}
            </CardContent>
          </Card>

          {payment.customer === null ? (
            <Card>
              <CardHeader>
                <CardTitle>Customer</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-slate-500 dark:text-slate-400">
                  The customer record was not included in this response.{' '}
                  <span className="font-mono text-xs">{payment.customer_id}</span>
                </p>
              </CardContent>
            </Card>
          ) : (
            <CustomerCard customer={payment.customer} />
          )}
        </div>
      </div>
    </div>
  );
}
