'use client';

/**
 * FailedPaymentsTable - the raw material the agent works on.
 *
 * One row per failed payment, and one action per row. The action is the whole
 * point of the table, so it is decided by data rather than by the operator
 * guessing: `recovery_case_id` is set once a payment has been analysed, so a
 * row either offers "Analyse with RecoverAI" (no case yet) or "View case" (one
 * exists). That field exists on `PaymentOut` precisely so this table never has
 * to make a second request to know which button to draw, and so two operators
 * cannot both start an analysis of the same payment.
 *
 * "Analyse" is a POST that runs the agent, opens a case and returns it, so on
 * success this navigates straight to the decision screen. The alternative -
 * analysing in place and leaving the operator to find the new case - adds a
 * step to the one flow this product is built around.
 *
 * Columns are ordered by what an operator scans for: the money, then who, then
 * what broke, then how stale it is. The failure reason is quoted verbatim from
 * the gateway with its error code underneath, because that pair is what a
 * merchant matches against their Razorpay dashboard.
 *
 * Client component: it owns per-row request state and navigates on success.
 */

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowRight, Inbox, WandSparkles } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";
import { formatRelativeTime, formatRupees } from "@/lib/format";
import { PAYMENT_METHOD_LABEL, type Payment } from "@/lib/types";
import { cn, errorMessage } from "@/lib/utils";

/**
 * What the gateway said, in the most specific form available.
 *
 * The fields are tried in descending order of usefulness to a human;
 * `error_code` is left out of this chain because it is rendered separately as
 * a monospace token. When every field is null the row says so rather than
 * showing an empty cell - a blank looks like a rendering bug, and "not
 * reported" is itself information about the gateway.
 */
function failureText(payment: Payment): string {
  return (
    payment.error_description ?? payment.error_reason ?? payment.error_step ?? "Not reported"
  );
}

export function FailedPaymentsTable({
  payments,
  className,
}: {
  payments: Payment[];
  className?: string;
}) {
  const router = useRouter();
  const [analysingId, setAnalysingId] = useState<string | null>(null);
  const [failure, setFailure] = useState<{ paymentId: string; message: string } | null>(null);

  async function analyse(paymentId: string): Promise<void> {
    setAnalysingId(paymentId);
    setFailure(null);

    try {
      const openedCase = await api.analyzePayment(paymentId);
      router.push(`/recovery/${openedCase.id}`);
      // `analysingId` is deliberately left set. The navigation is already in
      // flight; clearing it would flash the button back to its idle label for
      // the frame before the route changes, which reads as the click not
      // having worked.
    } catch (error) {
      setFailure({ paymentId, message: errorMessage(error) });
      setAnalysingId(null);
    }
  }

  // Derived here rather than read from the dashboard metrics so the number can
  // never disagree with the rows immediately beneath it.
  const unanalysed = payments.filter((payment) => payment.recovery_case_id === null).length;

  return (
    <Card className={className}>
      <CardHeader>
        <CardTitle>Failed payments</CardTitle>
        <CardDescription>
          {/* The unanalysed count is the operator's actual to-do number, so it
              leads. It is also what reconciles this table with the category
              breakdown above, which can only count failures the agent has
              already classified. */}
          {unanalysed > 0
            ? `${unanalysed} of ${payments.length} not analysed yet. Analysing one runs the agent and opens a recovery case; nothing is charged until you approve it.`
            : "Every failure the gateway reported has been analysed. Nothing is charged until you approve it."}
        </CardDescription>
      </CardHeader>

      <CardContent>
        {payments.length === 0 ? (
          <EmptyState
            icon={<Inbox className="h-5 w-5" />}
            title="No failed payments"
            description="Nothing has failed, which is the outcome this console exists to make rarer. Simulate a failure to see the agent work."
          />
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Amount</TableHead>
                <TableHead>Customer</TableHead>
                <TableHead>Method</TableHead>
                <TableHead>Failure</TableHead>
                <TableHead>Age</TableHead>
                <TableHead className="text-right">Action</TableHead>
              </TableRow>
            </TableHeader>

            <TableBody>
              {payments.map((payment) => {
                const rowFailure = failure?.paymentId === payment.id ? failure.message : null;
                // Narrowed into a local so the link below is provably non-null
                // rather than asserted with `!` on data that came off the wire.
                const caseId = payment.recovery_case_id;

                return (
                  <TableRow key={payment.id}>
                    <TableCell>
                      <Link
                        href={`/payments/${payment.id}`}
                        className="font-mono font-medium tabular-nums hover:underline"
                      >
                        {formatRupees(payment.amount_paise)}
                      </Link>
                      {payment.description ? (
                        <p className="mt-0.5 max-w-[16rem] truncate text-xs text-muted-foreground">
                          {payment.description}
                        </p>
                      ) : null}
                    </TableCell>

                    <TableCell>
                      {/* `customer` is nullable on the wire, so the id is the
                          fallback rather than an empty cell - an operator can
                          still match it against the gateway with the id alone. */}
                      <span className="text-sm">
                        {payment.customer ? payment.customer.name : payment.customer_id}
                      </span>
                      {payment.customer ? (
                        <p className="mt-0.5 max-w-[14rem] truncate text-xs text-muted-foreground">
                          {payment.customer.email}
                        </p>
                      ) : null}
                    </TableCell>

                    <TableCell>
                      <Badge>{PAYMENT_METHOD_LABEL[payment.method]}</Badge>
                    </TableCell>

                    <TableCell>
                      <span className="block max-w-[18rem] truncate text-sm">
                        {failureText(payment)}
                      </span>
                      {payment.error_code ? (
                        <code className="mt-0.5 block font-mono text-xs text-muted-foreground">
                          {payment.error_code}
                        </code>
                      ) : null}
                      {rowFailure ? (
                        <p role="alert" className="mt-1 text-xs text-danger-strong">
                          {rowFailure}
                        </p>
                      ) : null}
                    </TableCell>

                    <TableCell className="whitespace-nowrap text-sm text-muted-foreground">
                      {formatRelativeTime(payment.created_at)}
                    </TableCell>

                    <TableCell className="text-right">
                      {caseId === null ? (
                        <Button
                          size="sm"
                          variant="outline"
                          loading={analysingId === payment.id}
                          loadingText="Analysing…"
                          leadingIcon={<WandSparkles className="h-3.5 w-3.5" />}
                          // Only the row being analysed is disabled. Two
                          // analyses of different payments are independent
                          // server-side, and locking the whole table would stall
                          // an operator clearing a backlog.
                          onClick={() => void analyse(payment.id)}
                        >
                          Analyse with RecoverAI
                        </Button>
                      ) : (
                        // A real anchor rather than a button that calls
                        // `router.push`: an operator working a backlog opens
                        // cases in background tabs, and middle-click and
                        // "copy link" only work on an <a>.
                        <Link
                          href={`/recovery/${caseId}`}
                          className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}
                        >
                          View case
                          <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
                        </Link>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
