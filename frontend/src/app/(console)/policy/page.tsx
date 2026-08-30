"use client";

/**
 * `/policy` - the guardrail configuration, rendered read-only.
 *
 * This screen answers one question a reviewer will ask about any AI system that
 * touches money: what, concretely, is it not allowed to do? The answer is not
 * prose here - it is the live values the engine is running with, read from
 * `GET /api/policy` at the moment the page loads, plus the full catalogue of
 * rules that consume them.
 *
 * There is no edit control anywhere on this page, and that absence is the
 * feature. A limit that the automated path can raise is not a limit; if policy
 * were writable through the same API the agent's flow reaches, the guardrails
 * would be inside the blast radius of the thing they exist to contain. Changing
 * these values is a deployment action - an environment variable and a restart -
 * performed by a person with server access, which is a different and slower
 * kind of authority than a click.
 *
 * A client component rather than a server one, for two reasons: it keeps every
 * screen's loading and failure behaviour identical, and it means `next build`
 * never needs the FastAPI process to be running in order to prerender a page.
 */

import { useMemo, type ReactNode } from "react";
import { Ban, Lock, RefreshCw, ShieldCheck } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Tooltip } from "@/components/ui/tooltip";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { formatConfidence, formatRupees } from "@/lib/format";
import { FAILURE_CATEGORY_LABEL, type FailureCategory, type Policy } from "@/lib/types";

/** One row of the active-limits grid. */
interface LimitEntry {
  label: string;
  value: ReactNode;
  /** What the limit actually does, in the operator's language. */
  note: string;
  /**
   * The rule that consumes this value, tying it to the catalogue below.
   *
   * `null` for settings that are real policy but not numbered guardrail rules --
   * the recovery-link lifetime is enforced by the expiry sweep, not by a rule the
   * engine evaluates. Labelling it with a neighbouring rule id would be worse
   * than leaving it blank: an operator quoting "R11" from this screen would be
   * quoting a rule about payment age when they meant link expiry, and this page
   * exists precisely so those references are exact.
   */
  ruleId: string | null;
}

export default function PolicyPage() {
  const { data: policy, error, loading, refresh } = useApi(() => api.getPolicy(), []);

  const limits = useMemo(() => (policy === null ? [] : buildLimits(policy)), [policy]);

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
          Guardrails
        </h1>
        <p className="max-w-3xl text-sm leading-relaxed text-slate-600 dark:text-slate-400">
          Every recovery proposal is checked against the rules below by a deterministic engine
          running on the server - no model is consulted and no rule has a probability attached. The
          checks run twice: once when the agent proposes a strategy, and again at the moment a human
          clicks approve, because time passes in between and budgets, attempt counts and link
          expiries all move while a proposal sits in a queue. The most restrictive verdict always
          wins, so adding a rule can only ever make the system more conservative.
        </p>
      </header>

      <Alert variant="info" icon={<Lock className="h-4 w-4" aria-hidden="true" />}>
        <AlertTitle>This screen is read-only by design</AlertTitle>
        <AlertDescription>
          These values cannot be changed from the console or through the API the agent&apos;s flow
          uses. The AI has no tool that can read them into its own favour and none that can write
          them at all - the agent&apos;s entire toolset is read-only lookups plus a single tool that
          records a recommendation. A limit an automated system can raise is not a limit.
        </AlertDescription>
      </Alert>

      {error !== null ? (
        <Alert variant="danger">
          <AlertTitle>The guardrail configuration could not be read</AlertTitle>
          <AlertDescription className="space-y-3">
            <p>{error}</p>
            <Button
              variant="outline"
              size="sm"
              leadingIcon={<RefreshCw className="h-3.5 w-3.5" />}
              loading={loading}
              onClick={refresh}
            >
              Try again
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}

      {/* A skeleton that never resolves reads as a hung page. Once the read has
          definitively failed the alert above is the whole story, so the
          placeholders are withdrawn rather than left pulsing under it. */}
      {policy === null ? (
        error === null ? (
          <PolicySkeleton />
        ) : null
      ) : (
        <>
          <LimitsCard limits={limits} />
          <NonRecoverableCard categories={policy.non_recoverable_categories} />
          <RuleCatalogueCard policy={policy} />
        </>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Active limits                                                               */
/* -------------------------------------------------------------------------- */

/**
 * Turns the policy response into the display grid.
 *
 * Every money field is rendered through `formatRupees` from the paise integer,
 * never from the rupee float and never by dividing in this file. The paise value
 * is still worth showing, because it is the number the engine actually compares
 * against - so it lives in a tooltip on the amount rather than being dropped.
 */
function buildLimits(policy: Policy): LimitEntry[] {
  return [
    {
      label: "Maximum recovery attempts",
      value: <Plain>{policy.max_recovery_attempts}</Plain>,
      note: "How many times one failed payment may be retried before the case is closed for good.",
      ruleId: "R1_MAX_ATTEMPTS",
    },
    {
      label: "Attempt cooldown",
      value: <Plain>{policy.recovery_cooldown_seconds}s</Plain>,
      note: "Minimum wait between two attempts on the same payment, so a retry loop cannot run at machine speed.",
      ruleId: "R2_COOLDOWN",
    },
    {
      label: "Absolute amount ceiling",
      value: <Money paise={policy.max_recovery_amount_paise} />,
      note: "No recovery attempt may be created above this amount, whatever the agent recommends.",
      ruleId: "R4_AMOUNT_CEILING",
    },
    {
      label: "High-value review threshold",
      value: <Money paise={policy.high_value_review_threshold_paise} />,
      note: "At or above this amount the case always requires an explicit human approval.",
      ruleId: "R5_HIGH_VALUE_REVIEW",
    },
    {
      label: "Daily recovery budget",
      value: <Money paise={policy.daily_recovery_budget_paise} />,
      note: "Total value of recovery orders that may be created in one day across all customers.",
      ruleId: "R7_DAILY_BUDGET",
    },
    {
      label: "Cases per customer per day",
      value: <Plain>{policy.max_cases_per_customer_per_day}</Plain>,
      note: "Caps how often one customer can be re-presented in a single day.",
      ruleId: "R8_CUSTOMER_VELOCITY",
    },
    {
      label: "Minimum success likelihood",
      value: <Plain>{formatConfidence(policy.min_propensity_score)}</Plain>,
      note: "Below this predicted propensity a retry is judged not worth attempting and is denied.",
      ruleId: "R10_PROPENSITY_FLOOR",
    },
    {
      label: "Maximum payment age",
      value: <Plain>{policy.max_payment_age_hours}h</Plain>,
      note: "Older failures are stale - the instrument and the customer's intent have both likely moved on.",
      ruleId: "R11_PAYMENT_FRESHNESS",
    },
    {
      label: "Recovery link lifetime",
      value: <Plain>{policy.recovery_link_ttl_minutes} min</Plain>,
      note: "How long a payable link stays live before the case expires, bounding how long an open order can sit.",
      ruleId: null,
    },
    {
      label: "Human approval",
      value: (
        <Badge variant={policy.require_human_approval ? "success" : "warning"} dot>
          {policy.require_human_approval ? "Required for every rupee" : "Not universally required"}
        </Badge>
      ),
      note: policy.require_human_approval
        ? "The master switch is on: no money moves without a person clicking approve."
        : "The master switch is off, so the auto-approve lane below can apply.",
      ruleId: "R13_HUMAN_APPROVAL",
    },
    {
      label: "Auto-approve lane",
      value: (
        <Badge variant={policy.auto_approve_enabled ? "warning" : "neutral"} dot>
          {policy.auto_approve_enabled ? "Enabled" : "Disabled"}
        </Badge>
      ),
      note: "Optional graduated autonomy for small, high-confidence retries. Off in this deployment.",
      ruleId: "R13_HUMAN_APPROVAL",
    },
    {
      label: "Auto-approve ceiling",
      value: (
        <span className="flex flex-wrap items-baseline gap-2">
          <Money paise={policy.auto_approve_max_paise} />
          <span className="text-xs text-slate-500 dark:text-slate-400">
            at ≥ {formatConfidence(policy.auto_approve_min_propensity)} propensity
          </span>
        </span>
      ),
      note: "The envelope the auto-approve lane would be confined to if it were ever switched on.",
      ruleId: "R13_HUMAN_APPROVAL",
    },
  ];
}

function LimitsCard({ limits }: { limits: readonly LimitEntry[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Active limits</CardTitle>
        <CardDescription>
          The values the engine is running with right now, read live from the server.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {/* A <div> inside a <dl> may only wrap dt/dd pairs, so the note and the
            rule id live inside the <dd> rather than as siblings of it. */}
        <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {limits.map((limit) => (
            <div
              key={limit.label}
              className="rounded-lg border border-slate-200 p-4 dark:border-slate-800"
            >
              <dt className="text-xs font-medium text-slate-500 dark:text-slate-400">
                {limit.label}
              </dt>
              <dd className="mt-2">
                <span className="block text-lg font-semibold text-slate-900 dark:text-slate-50">
                  {limit.value}
                </span>
                <span className="mt-2 block text-xs leading-relaxed text-slate-500 dark:text-slate-400">
                  {limit.note}
                </span>
                <span className="mt-2 block font-mono text-2xs text-slate-400 dark:text-slate-500">
                  {limit.ruleId ?? "enforced by the expiry sweep, not by a rule"}
                </span>
              </dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}

/** Non-money scalars: mono and tabular so a column of them lines up. */
function Plain({ children }: { children: ReactNode }) {
  return <span className="font-mono tabular-nums">{children}</span>;
}

/**
 * A money limit, with the paise integer behind a tooltip.
 *
 * Rupees are the presentation unit and paise is the unit of record - the engine
 * compares integers, never floats, and this is where a reader can see both
 * without the screen having to explain the distinction twice. The trigger is a
 * focusable `<span>` with a dashed underline: the tooltip opens on focus as well
 * as hover, so the paise value is reachable from the keyboard rather than being
 * mouse-only trivia.
 */
function Money({ paise }: { paise: number }) {
  return (
    <Tooltip content={`${paise} paise - the integer value the engine compares against`}>
      <span
        tabIndex={0}
        className="cursor-help rounded-sm border-b border-dashed border-slate-300 font-mono tabular-nums dark:border-slate-700"
      >
        {formatRupees(paise)}
      </span>
    </Tooltip>
  );
}

/* -------------------------------------------------------------------------- */
/* Non-recoverable categories                                                  */
/* -------------------------------------------------------------------------- */

/**
 * The hard list.
 *
 * Given its own card rather than a row in the grid because it is categorically
 * different from the numbers above: those are thresholds a merchant might tune,
 * this is a set of failures where an automated retry is never the right answer
 * at any threshold. `risk_blocked` means a risk engine flagged the transaction -
 * re-presenting it is at best a wasted gateway call and at worst helps push a
 * stolen instrument through. `unknown` is on the list because absence of
 * evidence is not evidence of safety: we cannot reason about a failure we could
 * not classify, so it goes to a human instead.
 */
function NonRecoverableCard({ categories }: { categories: readonly FailureCategory[] }) {
  return (
    <Card className="border-rose-200 dark:border-rose-900">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Ban className="h-4 w-4 text-rose-600 dark:text-rose-400" aria-hidden="true" />
          Never recovered automatically
        </CardTitle>
        <CardDescription>
          Failure categories where no automated retry is offered, regardless of amount, propensity or
          budget. These are not thresholds - there is no value of any other setting that unlocks
          them.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap gap-2">
          {categories.map((category) => (
            <Badge key={category} variant="danger" dot>
              {FAILURE_CATEGORY_LABEL[category]}
            </Badge>
          ))}
        </div>
        <p className="text-xs leading-relaxed text-slate-500 dark:text-slate-400">
          A case in one of these categories is closed or escalated to a person. The engine records
          the refusal in the audit ledger with the rule that produced it, so a declined recovery is
          as inspectable as an approved one.
        </p>
      </CardContent>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Rule catalogue                                                              */
/* -------------------------------------------------------------------------- */

function RuleCatalogueCard({ policy }: { policy: Policy }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Rule catalogue</CardTitle>
        <CardDescription>
          {policy.rules.length} {policy.rules.length === 1 ? "rule" : "rules"}, evaluated in
          declaration order. Each returns allow, require-approval or deny; the most restrictive
          verdict across all of them becomes the case&apos;s decision.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Table containerClassName="rounded-lg border border-slate-200 dark:border-slate-800">
          <TableHeader>
            <TableRow>
              <TableHead className="w-56">Rule</TableHead>
              <TableHead className="w-64">Name</TableHead>
              <TableHead>What it checks</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {policy.rules.map((rule) => (
              <TableRow key={rule.rule_id}>
                <TableCell className="whitespace-nowrap font-mono text-xs text-slate-500 dark:text-slate-400">
                  {rule.rule_id}
                </TableCell>
                <TableCell className="text-sm font-medium text-slate-800 dark:text-slate-200">
                  {rule.name}
                </TableCell>
                <TableCell className="text-sm leading-relaxed text-slate-600 dark:text-slate-400">
                  {rule.description}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Loading                                                                     */
/* -------------------------------------------------------------------------- */

function PolicySkeleton() {
  return (
    <div className="space-y-6" aria-busy="true">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-slate-400" aria-hidden="true" />
            Active limits
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 6 }, (_unused, index) => (
              <Skeleton key={index} className="h-32 w-full" />
            ))}
          </div>
        </CardContent>
      </Card>
      <Skeleton className="h-64 w-full" />
    </div>
  );
}
