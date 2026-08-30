/**
 * FailureDetail - the recorded facts about the payment that failed.
 *
 * This is the anchor of both the payment screen and the recovery decision
 * screen, and it comes first on each of them on purpose: everything below it
 * (a classification, a model score, a guardrail verdict) is an *interpretation*
 * of what is shown here. An operator should be able to read the gateway's own
 * words before reading anything the system inferred from them.
 *
 * Two decisions worth naming:
 *
 * 1.  The gateway error is rendered verbatim, in monospace, field by field -
 *     `error_code`, `error_source`, `error_step`, `error_reason`,
 *     `error_description` - with the raw wire names kept rather than prettified.
 *     Those are the exact strings a merchant will paste into a Razorpay support
 *     ticket or grep for in their own logs, and a friendly rewrite would make
 *     the console's copy of the failure differ from everyone else's.
 *
 * 2.  Money is passed to `formatRupees` as integer paise. The API also sends a
 *     rupee float, and it is deliberately not used for display: one formatter,
 *     one unit, no component doing arithmetic on currency.
 */

import type { ReactNode } from 'react';
import { CreditCard, Receipt, ShieldAlert, TriangleAlert } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { formatDateTime, formatRupees } from '@/lib/format';
import {
  PAYMENT_METHOD_LABEL,
  PAYMENT_STATUS_LABEL,
  PAYMENT_STATUS_TONE,
  type Customer,
  type Payment,
} from '@/lib/types';
import { cn } from '@/lib/utils';

/**
 * The gateway error fields, in the order Razorpay itself reports them.
 *
 * The key type is narrowed to the five error columns rather than left as
 * `keyof Payment`, so `payment[key]` is a `string | null` the compiler can check
 * instead of a union of every field on the model.
 */
type PaymentErrorField =
  | 'error_code'
  | 'error_source'
  | 'error_step'
  | 'error_reason'
  | 'error_description';

const ERROR_FIELDS: readonly { key: PaymentErrorField; label: string }[] = [
  { key: 'error_code', label: 'error_code' },
  { key: 'error_source', label: 'error_source' },
  { key: 'error_step', label: 'error_step' },
  { key: 'error_reason', label: 'error_reason' },
  { key: 'error_description', label: 'error_description' },
];

export interface FailureDetailProps {
  /**
   * `null` when the API did not embed the payment on the case. Rendered as an
   * explicit gap rather than an empty card, because "we did not load it" and
   * "there was nothing to load" are different facts and only one of them is a
   * problem.
   */
  payment: Payment | null;
  /** Optional: the person behind the payment, shown as a compact summary strip. */
  customer?: Customer | null;
  title?: string;
  description?: string;
  className?: string;
}

/** One label/value pair in the facts grid. */
function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {label}
      </dt>
      <dd className="mt-0.5 truncate text-sm text-slate-900 dark:text-slate-100">{children}</dd>
    </div>
  );
}

export function FailureDetail({
  payment,
  customer,
  title = 'Original failure',
  description = 'What the gateway recorded, before anything was inferred from it.',
  className,
}: FailureDetailProps) {
  if (payment === null) {
    return (
      <Card className={className}>
        <CardHeader>
          <CardTitle>{title}</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-slate-500 dark:text-slate-400">
            The original payment was not included in this response. Open the payment directly to
            see the gateway&apos;s record of the failure.
          </p>
        </CardContent>
      </Card>
    );
  }

  // A payment can carry no error block at all - a captured payment never had
  // one. That is a normal state, not a missing value, so it is worded as a
  // statement of fact rather than shown as an error.
  const errorRows = ERROR_FIELDS.map(({ key, label }) => {
    const value = payment[key];
    return { label, value: typeof value === 'string' && value.length > 0 ? value : null };
  }).filter((row) => row.value !== null);

  return (
    <Card className={className}>
      <CardHeader
        action={
          <Badge variant={PAYMENT_STATUS_TONE[payment.status]} dot>
            {PAYMENT_STATUS_LABEL[payment.status]}
          </Badge>
        }
      >
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>

      <CardContent className="space-y-5">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="font-display text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
            {formatRupees(payment.amount_paise)}
          </span>
          <span className="text-xs text-slate-500 dark:text-slate-400">
            {payment.currency} &middot; {PAYMENT_METHOD_LABEL[payment.method]}
          </span>
        </div>

        {payment.description ? (
          <p className="text-sm leading-relaxed text-slate-600 dark:text-slate-300">
            {payment.description}
          </p>
        ) : null}

        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
          <Fact label="Payment id">
            <span className="font-mono text-xs">{payment.id}</span>
          </Fact>
          <Fact label="Method">{PAYMENT_METHOD_LABEL[payment.method]}</Fact>
          <Fact label="Failed at">{formatDateTime(payment.created_at)}</Fact>
          <Fact label="Order id">
            <span className="font-mono text-xs">{payment.razorpay_order_id ?? '—'}</span>
          </Fact>
          <Fact label="Gateway payment id">
            <span className="font-mono text-xs">{payment.razorpay_payment_id ?? '—'}</span>
          </Fact>
          <Fact label="Last updated">{formatDateTime(payment.updated_at)}</Fact>
        </dl>

        {payment.is_recovery_attempt ? (
          <p className="flex items-start gap-2 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-300">
            <Receipt className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span>
              This payment is itself a recovery attempt
              {payment.parent_payment_id === null ? (
                '.'
              ) : (
                <>
                  {' '}
                  for <span className="font-mono">{payment.parent_payment_id}</span>.
                </>
              )}
            </span>
          </p>
        ) : null}

        <Separator />

        <div>
          <p className="mb-2 flex items-center gap-2 text-xs font-medium text-slate-700 dark:text-slate-200">
            <TriangleAlert className="h-3.5 w-3.5 text-amber-600 dark:text-amber-400" aria-hidden="true" />
            Gateway error, verbatim
          </p>

          {errorRows.length === 0 ? (
            <p className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5 text-xs text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
              No gateway error was recorded on this payment.
            </p>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-slate-200 bg-slate-50 dark:border-slate-800 dark:bg-slate-900">
              <dl className="min-w-full divide-y divide-slate-200 font-mono text-xs dark:divide-slate-800">
                {errorRows.map((row) => (
                  <div key={row.label} className="flex gap-4 px-3 py-2">
                    <dt className="w-40 shrink-0 text-slate-500 dark:text-slate-400">{row.label}</dt>
                    <dd className="min-w-0 flex-1 break-words text-slate-800 dark:text-slate-200">
                      {row.value}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          )}
        </div>

        {customer === undefined || customer === null ? null : (
          <>
            <Separator />
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-slate-100 text-slate-500 dark:bg-slate-900 dark:text-slate-400">
                <CreditCard className="h-4 w-4" aria-hidden="true" />
              </span>
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">
                  {customer.name}
                </p>
                <p className="truncate text-xs text-slate-500 dark:text-slate-400">
                  {customer.email}
                  {customer.phone ? ` · ${customer.phone}` : ''}
                </p>
              </div>
              <div className="ml-auto flex items-center gap-2">
                <span className="text-xs text-slate-500 dark:text-slate-400">
                  {customer.successful_payments}/{customer.total_payments} paid before
                </span>
                {/* A risk flag is the one customer attribute that changes what
                    the system is allowed to do (rule R12 denies outright), so it
                    is the one that gets a badge rather than a line of text. */}
                {customer.risk_flagged ? (
                  <Badge
                    variant="danger"
                    icon={<ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />}
                  >
                    Risk flagged
                  </Badge>
                ) : null}
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
