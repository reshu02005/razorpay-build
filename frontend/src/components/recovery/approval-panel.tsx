'use client';

/**
 * ApprovalPanel - the only control in this console that causes money to move.
 *
 * ---------------------------------------------------------------------------
 * The enabled state comes from the server. All of it.
 * ---------------------------------------------------------------------------
 * `can_approve`, `can_reject` and `approval_blocked_reason` are computed by the
 * guardrail engine and arrive on the case. This component renders them and does
 * not reason about them: there is no `status === 'awaiting_approval'` check
 * behind the Approve button, no local copy of the state machine, no second
 * opinion about whether the daily budget has room.
 *
 * That restraint is the whole point. If the button's enabled state were derived
 * in React, the frontend would become a second implementation of the policy -
 * and the two would drift, because they always do. When they drift, the one the
 * operator sees is the wrong one, and the failure mode is an approval that
 * looked permitted right up until it moved someone's money. Rendering a flag is
 * boring and cannot drift.
 *
 * The server does not trust this panel either: `approve()` re-evaluates all
 * thirteen rules against live state before creating an order, so a case that
 * was approvable when the page loaded can still be refused seconds later. When
 * that happens the API's own message is the most informative sentence on the
 * screen, and it is rendered verbatim rather than replaced with a generic
 * "something went wrong".
 *
 * ---------------------------------------------------------------------------
 * The confirmation step is the feature
 * ---------------------------------------------------------------------------
 * Approving opens a dialog that restates the amount, the customer and the
 * strategy before anything is sent. That is not friction to be optimised away:
 * creating a payment order is irreversible from this console, and the last
 * moment at which a misread row can be caught is the moment before the request
 * leaves. The dialog exists so an operator reads the amount at least once with
 * their finger off the button.
 *
 * Rejection requires a typed reason for a different reason: a rejection with no
 * reason teaches the next reviewer, and the system, nothing at all.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import {
  Check,
  Copy,
  ExternalLink,
  Link2,
  ShieldCheck,
  TriangleAlert,
  XCircle,
} from 'lucide-react';

import { CaseStatusPill } from '@/components/recovery/case-status-pill';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button, buttonVariants } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Dialog, DialogFooter } from '@/components/ui/dialog';
import { Separator } from '@/components/ui/separator';
import { ApiRequestError, api } from '@/lib/api';
import { formatDateTime, formatRupees } from '@/lib/format';
import {
  RECOVERY_STATUS_TONE,
  RECOVERY_STRATEGY_LABEL,
  type RecoveryCase,
} from '@/lib/types';
import { cn, errorMessage } from '@/lib/utils';

/**
 * Where the operator's name is remembered between sessions.
 *
 * A merchant reviewing a queue types the same name into every case, and making
 * them retype it is how you end up with "a", "x" and "asdf" in an audit trail
 * that is supposed to answer "who approved it?". It is a convenience only: the
 * value is sent with the request and recorded by the server, which is the copy
 * that counts. `localStorage` throws outright in a browser configured to block
 * site data, so every access is wrapped.
 */
const OPERATOR_STORAGE_KEY = 'recoverai-operator';

/** Bounds mirrored from `ApproveIn` / `RejectIn` so the API never has to reject
 *  a length the field could have prevented. This is input validation, not a
 *  second copy of the approval policy. */
const OPERATOR_MAX_LENGTH = 120;
const REASON_MAX_LENGTH = 500;
const NOTE_MAX_LENGTH = 500;

type PendingAction = 'approve' | 'reject' | 'abandon';

interface PanelError {
  message: string;
  /** Stable machine code (`guardrail_denied`, `invalid_transition`, ...). */
  code: string | null;
}

function toPanelError(err: unknown): PanelError {
  if (err instanceof ApiRequestError) return { message: err.message, code: err.code };
  return { message: errorMessage(err), code: null };
}

function readStoredOperator(): string {
  try {
    return window.localStorage.getItem(OPERATOR_STORAGE_KEY) ?? '';
  } catch {
    return '';
  }
}

function writeStoredOperator(value: string): void {
  try {
    window.localStorage.setItem(OPERATOR_STORAGE_KEY, value);
  } catch {
    // A remembered name is never worth breaking an approval over.
  }
}

/** Shared styling for the two free-text inputs in this panel. */
const FIELD_CLASSES =
  'w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-100 dark:placeholder:text-slate-600';

const LABEL_CLASSES =
  'mb-1 block text-2xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400';

export interface ApprovalPanelProps {
  recoveryCase: RecoveryCase;
  /** Called after any successful state change so the page can refetch the case
   *  and the ledger. The panel deliberately does not hold its own copy of the
   *  post-decision case - one source of truth per screen. */
  onDecision: () => void;
  className?: string;
}

export function ApprovalPanel({ recoveryCase, onDecision, className }: ApprovalPanelProps) {
  const [operator, setOperator] = useState('');
  const [note, setNote] = useState('');
  const [rejectFormOpen, setRejectFormOpen] = useState(false);
  const [rejectReason, setRejectReason] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [panelError, setPanelError] = useState<PanelError | null>(null);
  const [copied, setCopied] = useState(false);

  const checkoutPath = `/checkout/${recoveryCase.id}`;
  // Seeded with the relative path so the field is never empty during the server
  // render, then upgraded to an absolute URL after mount. `window` does not
  // exist on the server, and a value that differed between the two passes would
  // be a hydration mismatch on a field the operator is about to copy.
  const [recoveryLink, setRecoveryLink] = useState(checkoutPath);

  const copyTimer = useRef<number | null>(null);

  useEffect(() => {
    setOperator(readStoredOperator());
  }, []);

  useEffect(() => {
    setRecoveryLink(`${window.location.origin}${checkoutPath}`);
  }, [checkoutPath]);

  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    },
    [],
  );

  const operatorName = operator.trim();
  const operatorMissing = operatorName.length === 0;
  const awaitingPayment = recoveryCase.status === 'awaiting_payment';
  const busy = pending !== null;

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(recoveryLink);
      setCopied(true);
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // The Clipboard API is unavailable over plain HTTP on a non-local host and
      // can be denied by permission. Saying so beats a button that silently
      // does nothing.
      setPanelError({
        message: 'The browser refused clipboard access. Select the link and copy it manually.',
        code: null,
      });
    }
  }, [recoveryLink]);

  const handleApprove = async () => {
    setPanelError(null);
    setPending('approve');
    try {
      writeStoredOperator(operatorName);
      const trimmedNote = note.trim();
      await api.approveCase(recoveryCase.id, {
        approved_by: operatorName,
        // `undefined` rather than an empty string: `JSON.stringify` drops the
        // key entirely, so an operator who typed nothing does not store a blank
        // note against a decision in the ledger.
        note: trimmedNote.length > 0 ? trimmedNote : undefined,
      });
      setConfirmOpen(false);
      setNote('');
      onDecision();
    } catch (err: unknown) {
      // The dialog stays open on failure. The most common failure here is the
      // server re-evaluating guardrails and denying, and that message belongs
      // next to the amount it refused.
      setPanelError(toPanelError(err));
    } finally {
      setPending(null);
    }
  };

  const handleReject = async () => {
    setPanelError(null);
    setPending('reject');
    try {
      writeStoredOperator(operatorName);
      await api.rejectCase(recoveryCase.id, {
        rejected_by: operatorName,
        reason: rejectReason.trim(),
      });
      setRejectFormOpen(false);
      setRejectReason('');
      onDecision();
    } catch (err: unknown) {
      setPanelError(toPanelError(err));
    } finally {
      setPending(null);
    }
  };

  const handleAbandon = async () => {
    setPanelError(null);
    setPending('abandon');
    try {
      await api.markAttemptFailed(recoveryCase.id, {
        reason: 'Customer did not complete the payment',
      });
      onDecision();
    } catch (err: unknown) {
      setPanelError(toPanelError(err));
    } finally {
      setPending(null);
    }
  };

  const decisionRecorded =
    recoveryCase.approved_by !== null ||
    recoveryCase.rejected_by !== null ||
    recoveryCase.recovered_at !== null ||
    recoveryCase.failure_note !== null;

  /*
   * Built once and rendered in one of two places, never both: inside the
   * confirmation dialog while it is open, in the panel body otherwise. The
   * dialog is a full-screen overlay, so an approval that fails server-side
   * would otherwise print its refusal behind the modal the operator is still
   * looking at - which is the same as not printing it at all.
   */
  const errorAlert =
    panelError === null ? null : (
      <Alert variant="danger">
        <AlertTitle className="flex flex-wrap items-center gap-2">
          Request refused
          {panelError.code === null ? null : (
            <span className="rounded bg-rose-100 px-1.5 py-0.5 font-mono text-2xs font-normal text-rose-800 dark:bg-rose-900/60 dark:text-rose-200">
              {panelError.code}
            </span>
          )}
        </AlertTitle>
        {/* Verbatim. When the server re-runs the guardrails at approval time and
            denies, this sentence names the rule and the number that stopped it -
            there is nothing more useful to show. */}
        <AlertDescription>{panelError.message}</AlertDescription>
      </Alert>
    );

  return (
    <Card className={className}>
      <CardHeader action={<CaseStatusPill status={recoveryCase.status} />}>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheck className="h-4 w-4 text-slate-500 dark:text-slate-400" aria-hidden="true" />
          Human approval
        </CardTitle>
        <CardDescription>
          Nothing in this system creates a payment order without a decision recorded here.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
          <div>
            <dt className="text-2xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Amount
            </dt>
            <dd className="mt-0.5 font-display text-lg font-semibold tracking-tight text-slate-900 dark:text-slate-50">
              {formatRupees(recoveryCase.amount_paise)}
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="text-2xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Strategy
            </dt>
            <dd className="mt-0.5 truncate text-sm text-slate-900 dark:text-slate-100">
              {RECOVERY_STRATEGY_LABEL[recoveryCase.strategy]}
            </dd>
          </div>
          <div className="col-span-2 min-w-0">
            <dt className="text-2xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Customer
            </dt>
            <dd className="mt-0.5 truncate text-sm text-slate-900 dark:text-slate-100">
              {recoveryCase.customer_name || recoveryCase.customer_id}
              {recoveryCase.customer === null ? null : (
                <span className="text-slate-500 dark:text-slate-400">
                  {' '}
                  &middot; {recoveryCase.customer.email}
                </span>
              )}
            </dd>
          </div>
        </dl>

        {confirmOpen ? null : errorAlert}

        {awaitingPayment ? (
          <div className="space-y-3">
            <Alert variant="warning">
              <AlertTitle>Order live - waiting on the customer</AlertTitle>
              <AlertDescription>
                The recovery order has been created for {formatRupees(recoveryCase.amount_paise)}.
                {recoveryCase.expires_at === null
                  ? ' It has no expiry set.'
                  : ` The link expires ${formatDateTime(recoveryCase.expires_at)}.`}
              </AlertDescription>
            </Alert>

            <div>
              <span className={LABEL_CLASSES}>Recovery link</span>
              <div className="flex gap-2">
                <input
                  readOnly
                  value={recoveryLink}
                  aria-label="Recovery link"
                  onFocus={(event) => event.currentTarget.select()}
                  className={cn(FIELD_CLASSES, 'font-mono text-xs')}
                />
                <Button
                  variant="outline"
                  onClick={() => {
                    void handleCopy();
                  }}
                  leadingIcon={
                    copied ? (
                      <Check className="h-4 w-4" aria-hidden="true" />
                    ) : (
                      <Copy className="h-4 w-4" aria-hidden="true" />
                    )
                  }
                >
                  {copied ? 'Copied' : 'Copy'}
                </Button>
              </div>
            </div>

            <Link
              href={checkoutPath}
              target="_blank"
              rel="noopener noreferrer"
              className={cn(buttonVariants({ variant: 'default' }), 'w-full')}
            >
              <Link2 className="h-4 w-4" aria-hidden="true" />
              Open customer checkout
              <ExternalLink className="h-3.5 w-3.5 opacity-70" aria-hidden="true" />
            </Link>

            <Separator />

            <div>
              <Button
                variant="outline"
                className="w-full"
                loading={pending === 'abandon'}
                loadingText="Marking failed…"
                disabled={busy}
                onClick={() => {
                  void handleAbandon();
                }}
              >
                Simulate customer abandonment
              </Button>
              {/* A demo that can only show the happy path is a demo that has not
                  been tested. This marks the live attempt failed on purpose, so
                  the failure branch of the state machine - and the ledger entry
                  it writes - can be shown deliberately rather than hoped for. */}
              <p className="mt-1.5 text-2xs leading-relaxed text-slate-500 dark:text-slate-400">
                Forces the current attempt to fail so the failure path can be demonstrated. No
                money is involved either way.
              </p>
            </div>
          </div>
        ) : recoveryCase.can_approve || recoveryCase.can_reject ? (
          <div className="space-y-3">
            <div>
              <label className={LABEL_CLASSES} htmlFor="approval-operator">
                Operator
              </label>
              <input
                id="approval-operator"
                value={operator}
                onChange={(event) => setOperator(event.target.value)}
                maxLength={OPERATOR_MAX_LENGTH}
                autoComplete="off"
                placeholder="Your name, for the audit trail"
                className={FIELD_CLASSES}
              />
              {operatorMissing ? (
                <p className="mt-1 text-2xs text-slate-500 dark:text-slate-400">
                  Required: the ledger records who decided, not that someone did.
                </p>
              ) : null}
            </div>

            <div className="flex flex-wrap gap-2">
              <Button
                variant="success"
                className="flex-1"
                // Two separate reasons to be unavailable, kept distinct: the
                // server says this case cannot be approved, or the form is not
                // filled in yet. Only the first is policy.
                disabled={!recoveryCase.can_approve || operatorMissing || busy}
                onClick={() => {
                  setPanelError(null);
                  setConfirmOpen(true);
                }}
                leadingIcon={<ShieldCheck className="h-4 w-4" aria-hidden="true" />}
              >
                Approve
              </Button>
              <Button
                variant="outline"
                className="flex-1"
                disabled={!recoveryCase.can_reject || busy}
                onClick={() => {
                  setPanelError(null);
                  setRejectFormOpen((open) => !open);
                }}
                leadingIcon={<XCircle className="h-4 w-4" aria-hidden="true" />}
              >
                Reject
              </Button>
            </div>

            {rejectFormOpen ? (
              <div className="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
                <label className={LABEL_CLASSES} htmlFor="rejection-reason">
                  Why are you rejecting this?
                </label>
                <textarea
                  id="rejection-reason"
                  rows={3}
                  value={rejectReason}
                  onChange={(event) => setRejectReason(event.target.value)}
                  maxLength={REASON_MAX_LENGTH}
                  placeholder="e.g. Customer already paid by bank transfer."
                  className={cn(FIELD_CLASSES, 'resize-y')}
                />
                <div className="mt-2 flex justify-end gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    disabled={busy}
                    onClick={() => {
                      setRejectFormOpen(false);
                      setRejectReason('');
                    }}
                  >
                    Cancel
                  </Button>
                  <Button
                    variant="danger"
                    size="sm"
                    loading={pending === 'reject'}
                    loadingText="Rejecting…"
                    disabled={rejectReason.trim().length === 0 || operatorMissing || busy}
                    onClick={() => {
                      void handleReject();
                    }}
                  >
                    Confirm rejection
                  </Button>
                </div>
              </div>
            ) : null}

            {recoveryCase.approval_blocked_reason === null ? null : (
              <Alert variant="warning">
                <AlertDescription>{recoveryCase.approval_blocked_reason}</AlertDescription>
              </Alert>
            )}
          </div>
        ) : (
          <Alert variant={RECOVERY_STATUS_TONE[recoveryCase.status]}>
            <AlertTitle>No decision is open on this case</AlertTitle>
            <AlertDescription>
              {/* The server's own words when it has them. The fallback describes
                  the status it sent rather than inferring a reason, because
                  guessing at policy is exactly what this panel refuses to do. */}
              {recoveryCase.approval_blocked_reason ??
                'The case is not waiting on an approval decision right now.'}
            </AlertDescription>
          </Alert>
        )}

        {decisionRecorded ? (
          <>
            <Separator />
            <dl className="space-y-2 text-xs">
              {recoveryCase.approved_by === null ? null : (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0 text-slate-500 dark:text-slate-400">Approved by</dt>
                  <dd className="min-w-0 flex-1 text-slate-800 dark:text-slate-200">
                    {recoveryCase.approved_by}
                    <span className="text-slate-500 dark:text-slate-400">
                      {' '}
                      &middot; {formatDateTime(recoveryCase.approved_at)}
                    </span>
                  </dd>
                </div>
              )}
              {recoveryCase.rejected_by === null ? null : (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0 text-slate-500 dark:text-slate-400">Rejected by</dt>
                  <dd className="min-w-0 flex-1 text-slate-800 dark:text-slate-200">
                    {recoveryCase.rejected_by}
                    <span className="text-slate-500 dark:text-slate-400">
                      {' '}
                      &middot; {formatDateTime(recoveryCase.rejected_at)}
                    </span>
                    {recoveryCase.rejection_reason === null ? null : (
                      <p className="mt-0.5 text-slate-600 dark:text-slate-300">
                        &ldquo;{recoveryCase.rejection_reason}&rdquo;
                      </p>
                    )}
                  </dd>
                </div>
              )}
              {recoveryCase.recovered_at === null ? null : (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0 text-slate-500 dark:text-slate-400">Recovered</dt>
                  <dd className="min-w-0 flex-1 text-emerald-700 dark:text-emerald-300">
                    {formatRupees(recoveryCase.recovered_amount_paise)}
                    <span className="text-slate-500 dark:text-slate-400">
                      {' '}
                      &middot; {formatDateTime(recoveryCase.recovered_at)}
                    </span>
                  </dd>
                </div>
              )}
              {recoveryCase.failure_note === null ? null : (
                <div className="flex gap-2">
                  <dt className="w-24 shrink-0 text-slate-500 dark:text-slate-400">Failure</dt>
                  <dd className="min-w-0 flex-1 text-slate-800 dark:text-slate-200">
                    {recoveryCase.failure_note}
                  </dd>
                </div>
              )}
            </dl>
          </>
        ) : null}
      </CardContent>

      {/*
       * The confirmation. It restates the three facts an operator can misread
       * from a dense screen - how much, to whom, and doing what - because this
       * is the last point at which a wrong row can still be caught.
       */}
      <Dialog
        open={confirmOpen}
        onOpenChange={(next: boolean) => {
          if (pending === 'approve') return;
          setConfirmOpen(next);
        }}
        title="Approve this recovery?"
        description="Approving creates a live payment order. Guardrails are re-evaluated on the server at this moment, so approval can still be refused."
        // A stray click on the backdrop must not silently cancel a decision the
        // operator has already committed to reading. They answer it deliberately.
        closeOnOverlayClick={false}
        footer={
          <DialogFooter>
            <Button
              variant="outline"
              disabled={pending === 'approve'}
              onClick={() => setConfirmOpen(false)}
            >
              Cancel
            </Button>
            <Button
              variant="success"
              // `loading` disables the button on the same render that shows the
              // spinner, so an impatient second click lands on a dead control
              // instead of submitting a second approval.
              loading={pending === 'approve'}
              loadingText="Approving…"
              onClick={() => {
                void handleApprove();
              }}
            >
              Approve and create order
            </Button>
          </DialogFooter>
        }
      >
        <div className="space-y-4">
          {errorAlert}

          <dl className="divide-y divide-slate-200 rounded-lg border border-slate-200 dark:divide-slate-800 dark:border-slate-800">
            <div className="flex items-baseline justify-between gap-4 px-3 py-2.5">
              <dt className="text-xs text-slate-500 dark:text-slate-400">Amount</dt>
              <dd className="font-display text-lg font-semibold tracking-tight text-slate-900 dark:text-slate-50">
                {formatRupees(recoveryCase.amount_paise)}
              </dd>
            </div>
            <div className="flex items-baseline justify-between gap-4 px-3 py-2.5">
              <dt className="text-xs text-slate-500 dark:text-slate-400">Customer</dt>
              <dd className="min-w-0 truncate text-sm text-slate-900 dark:text-slate-100">
                {recoveryCase.customer_name || recoveryCase.customer_id}
              </dd>
            </div>
            <div className="flex items-baseline justify-between gap-4 px-3 py-2.5">
              <dt className="text-xs text-slate-500 dark:text-slate-400">Strategy</dt>
              <dd className="min-w-0 truncate text-sm text-slate-900 dark:text-slate-100">
                {RECOVERY_STRATEGY_LABEL[recoveryCase.strategy]}
              </dd>
            </div>
            <div className="flex items-baseline justify-between gap-4 px-3 py-2.5">
              <dt className="text-xs text-slate-500 dark:text-slate-400">Approving as</dt>
              <dd className="min-w-0 truncate text-sm text-slate-900 dark:text-slate-100">
                {operatorName}
              </dd>
            </div>
          </dl>

          <div>
            <label className={LABEL_CLASSES} htmlFor="approval-note">
              Note (optional)
            </label>
            <textarea
              id="approval-note"
              rows={2}
              value={note}
              onChange={(event) => setNote(event.target.value)}
              maxLength={NOTE_MAX_LENGTH}
              placeholder="Context for whoever reads this in the ledger later."
              className={cn(FIELD_CLASSES, 'resize-y')}
            />
          </div>

          <p className="flex items-start gap-2 text-2xs leading-relaxed text-slate-500 dark:text-slate-400">
            <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            The amount is copied from the original payment and cannot be changed here or by the
            agent.
          </p>
        </div>
      </Dialog>
    </Card>
  );
}
