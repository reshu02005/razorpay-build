"use client";

/**
 * `/audit` - the tamper-evident ledger, and the button that proves it.
 *
 * Every other screen in this console makes a claim: the agent classified this,
 * the guardrails allowed that, a human approved it at 14:05. This screen is
 * where those claims are checked. It has two halves and the order matters:
 *
 *  1.  The verification panel, first and unmissable. `GET /api/audit/verify`
 *      recomputes every hash in the chain from genesis and reports either an
 *      intact chain and its head, or the exact sequence number where the
 *      recomputation stopped matching.
 *  2.  The ledger itself, newest first, with each entry's payload expandable.
 *
 * Why verification is a *button* and not a static badge: a system that only
 * asserts its own immutability has proved nothing at all. A reviewer must be
 * able to press something, watch a request go out, and see the answer come back
 * computed rather than remembered. The claim is only worth what the check is,
 * so the check is the loudest element on the page.
 *
 * The screen is a client component because it filters, expands rows and re-runs
 * verification on demand. Reading the ledger costs one request either way.
 */

import { Suspense, useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import {
  ChevronDown,
  ChevronRight,
  Link2,
  RefreshCw,
  ScrollText,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge, type BadgeVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useApi } from "@/hooks/useApi";
import { api } from "@/lib/api";
import { formatDateTime, truncateId } from "@/lib/format";
import {
  ACTOR_TYPE_LABEL,
  AUDIT_EVENT_LABEL,
  type ActorType,
  type AuditEvent,
  type AuditEventType,
} from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * How many entries to pull. The ledger is append-only and grows with every
 * analysis, so an unbounded read would eventually be a very large response for a
 * table nobody scrolls to the bottom of. 500 comfortably covers a seeded
 * database plus a demo session, and the count is stated on the card so the
 * ceiling is never mistaken for the whole ledger.
 */
const LEDGER_LIMIT = 500;

/** The `prev_hash` of the first entry: 64 zeroes, matching `GENESIS_HASH`. */
const GENESIS_HASH = "0".repeat(64);

/** Sentinel for "no filter" in the two dropdowns. */
const ANY = "all";

/**
 * Badge tone per ledger event.
 *
 * Exhaustive by type, for the same reason the label maps in `@/lib/types` are:
 * a new audit event added on the server should be a compile error here until
 * someone has decided what it means, not a row that silently renders grey.
 *
 * Colour is spent narrowly. Only money actually arriving is emerald; only a
 * genuine refusal or failure is rose; the honesty events - the agent falling
 * back to rules, the gateway being simulated - are amber, because they are
 * things a reviewer should notice without being alarmed by.
 */
const EVENT_VARIANT: Record<AuditEventType, BadgeVariant> = {
  payment_failed: "danger",
  analysis_started: "neutral",
  failure_classified: "ai",
  propensity_scored: "ai",
  strategy_proposed: "ai",
  guardrails_evaluated: "info",
  recovery_blocked: "danger",
  approval_requested: "warning",
  approval_granted: "info",
  approval_rejected: "danger",
  recovery_order_created: "info",
  recovery_link_sent: "info",
  payment_verified: "info",
  recovery_succeeded: "success",
  recovery_failed: "danger",
  recovery_expired: "neutral",
  webhook_received: "neutral",
  agent_degraded: "warning",
  gateway_simulated: "warning",
};

/** `agent` gets the AI tint so machine-authored entries are separable at a glance. */
const ACTOR_VARIANT: Record<ActorType, BadgeVariant> = {
  agent: "ai",
  human: "info",
  system: "neutral",
  webhook: "neutral",
};

/**
 * Options for the two dropdowns, derived from the label maps rather than typed
 * out again. `Object.entries` yields `[string, string]` pairs, which is exactly
 * what a `<select>` wants - and keeping the filter state as a plain string means
 * the change handler needs no cast to a union it cannot actually guarantee.
 */
const EVENT_TYPE_OPTIONS: ReadonlyArray<[string, string]> = Object.entries(AUDIT_EVENT_LABEL);
const ACTOR_TYPE_OPTIONS: ReadonlyArray<[string, string]> = Object.entries(ACTOR_TYPE_LABEL);

/**
 * `useSearchParams` opts a route into client-side rendering, and Next requires
 * that bail-out to sit behind a Suspense boundary. Wrapping here rather than
 * reading `window.location` keeps the `?case_id=` deep link working as a normal
 * navigation from the case screen.
 */
export default function AuditPage() {
  return (
    <Suspense fallback={<LedgerFallback />}>
      <AuditLedgerScreen />
    </Suspense>
  );
}

function LedgerFallback() {
  return (
    <div className="space-y-6" aria-busy="true">
      <Skeleton className="h-24 w-full" />
      <Skeleton className="h-96 w-full" />
    </div>
  );
}

function AuditLedgerScreen() {
  const searchParams = useSearchParams();
  const caseIdParam = searchParams.get("case_id") ?? "";

  // Two pieces of state, not one: `draft` is what the operator is typing and
  // `applied` is what the server has been asked for. Filtering by case id is a
  // round trip, so committing on submit rather than on every keystroke is the
  // difference between one request and thirty-six.
  const [caseIdDraft, setCaseIdDraft] = useState(caseIdParam);
  const [caseIdFilter, setCaseIdFilter] = useState(caseIdParam);

  // Arriving from a case screen is a client-side navigation, which may reuse
  // this component rather than remounting it - so the query parameter has to be
  // watched, not just read once at mount.
  useEffect(() => {
    setCaseIdDraft(caseIdParam);
    setCaseIdFilter(caseIdParam);
  }, [caseIdParam]);

  const [eventTypeFilter, setEventTypeFilter] = useState<string>(ANY);
  const [actorTypeFilter, setActorTypeFilter] = useState<string>(ANY);

  const chain = useApi(() => api.verifyAuditChain(), []);
  const ledger = useApi(
    () =>
      api.listAuditEvents({
        caseId: caseIdFilter === "" ? undefined : caseIdFilter,
        limit: LEDGER_LIMIT,
      }),
    [caseIdFilter],
  );

  const events = ledger.data;

  const visible = useMemo(() => {
    if (events === null) return [];
    return events
      .filter((event) => eventTypeFilter === ANY || event.event_type === eventTypeFilter)
      .filter((event) => actorTypeFilter === ANY || event.actor_type === actorTypeFilter)
      // Newest first. Sorted here rather than trusted from the endpoint because
      // `sequence` - not arrival order and not `created_at` - is the ledger's
      // own notion of position, and it is the column the hash chain is built on.
      .sort((a, b) => b.sequence - a.sequence);
  }, [events, eventTypeFilter, actorTypeFilter]);

  const filtersActive = eventTypeFilter !== ANY || actorTypeFilter !== ANY || caseIdFilter !== "";

  const clearFilters = useCallback(() => {
    setEventTypeFilter(ANY);
    setActorTypeFilter(ANY);
    setCaseIdDraft("");
    setCaseIdFilter("");
  }, []);

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="text-xl font-semibold tracking-tight text-slate-900 dark:text-slate-50">
          Audit ledger
        </h1>
        {/* The mechanism is explained on the page, not in a doc nobody opens.
            Someone who understands why editing history is detectable will read
            the table below differently from someone told to trust it. */}
        <p className="max-w-3xl text-sm leading-relaxed text-slate-600 dark:text-slate-400">
          Every action this system takes is appended here and never edited. Each entry stores a
          SHA-256 hash of its own contents <em>plus the hash of the entry before it</em>, so the
          entries form a chain rather than a list. Change one field in one historical row and its
          hash changes; the next row&apos;s stored <span className="font-mono text-xs">prev_hash</span>{" "}
          stops matching, and every hash after that one breaks with it. Rewriting the past without
          leaving a trace would mean recomputing the whole chain from that point forward - which is
          exactly what the check below does, and reports.
        </p>
      </header>

      <ChainPanel
        valid={chain.data?.valid ?? null}
        eventsChecked={chain.data?.events_checked ?? null}
        headHash={chain.data?.head_hash ?? null}
        brokenAtSequence={chain.data?.broken_at_sequence ?? null}
        message={chain.data?.message ?? null}
        error={chain.error}
        loading={chain.loading}
        onReverify={chain.refresh}
      />

      <Card>
        <CardHeader
          action={
            <Button
              variant="ghost"
              size="sm"
              leadingIcon={<RefreshCw className="h-3.5 w-3.5" />}
              loading={ledger.loading && events !== null}
              onClick={ledger.refresh}
            >
              Refresh
            </Button>
          }
        >
          <CardTitle>Ledger entries</CardTitle>
          <CardDescription>
            {events !== null
              ? `Showing ${visible.length} of ${events.length} loaded ${
                  events.length === 1 ? "entry" : "entries"
                }, newest first. Reads are capped at ${LEDGER_LIMIT}.`
              : ledger.error !== null
                ? "The ledger has not been read."
                : "Reading the ledger…"}
          </CardDescription>
        </CardHeader>

        <CardContent className="space-y-4">
          <FilterBar
            caseIdDraft={caseIdDraft}
            onCaseIdDraftChange={setCaseIdDraft}
            onCaseIdSubmit={() => setCaseIdFilter(caseIdDraft.trim())}
            eventTypeFilter={eventTypeFilter}
            onEventTypeChange={setEventTypeFilter}
            actorTypeFilter={actorTypeFilter}
            onActorTypeChange={setActorTypeFilter}
            filtersActive={filtersActive}
            onClear={clearFilters}
          />

          {ledger.error !== null ? (
            <Alert variant="danger">
              <AlertTitle>The ledger could not be read</AlertTitle>
              <AlertDescription>{ledger.error}</AlertDescription>
            </Alert>
          ) : null}

          {/* A failed read is NOT an empty ledger, and must never be reported as
              one: "the ledger is empty" on a screen whose whole purpose is
              evidence would be the most damaging sentence on the page. When the
              read failed the alert above is the entire answer. */}
          {events === null && ledger.error !== null ? null : events === null && ledger.loading ? (
            <div className="space-y-2" aria-busy="true">
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
            </div>
          ) : visible.length === 0 ? (
            <EmptyState
              icon={<ScrollText className="h-5 w-5" aria-hidden="true" />}
              title={filtersActive ? "No entries match these filters" : "The ledger is empty"}
              description={
                filtersActive
                  ? "The ledger is append-only, so nothing has been hidden - these filters simply exclude every entry loaded. Clear them to see the full chain."
                  : "Nothing has happened yet. Analysing a failed payment writes the first entries: the classification, the propensity score, the strategy and the guardrail verdict."
              }
              action={
                filtersActive ? (
                  <Button variant="outline" size="sm" onClick={clearFilters}>
                    Clear filters
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <LedgerTable events={visible} />
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Chain verification                                                          */
/* -------------------------------------------------------------------------- */

interface ChainPanelProps {
  valid: boolean | null;
  eventsChecked: number | null;
  headHash: string | null;
  brokenAtSequence: number | null;
  message: string | null;
  error: string | null;
  loading: boolean;
  onReverify: () => void;
}

/**
 * The verdict panel.
 *
 * Written as one strong sentence rather than a row of metrics. An auditor should
 * be able to glance at this and know the answer without reading a legend: green
 * and "hash chain intact" is a pass, red and a named sequence number is a fail.
 * The head hash is shown in full-width mono because it is the value someone
 * would actually copy in order to check it independently later.
 */
function ChainPanel({
  valid,
  eventsChecked,
  headHash,
  brokenAtSequence,
  message,
  error,
  loading,
  onReverify,
}: ChainPanelProps) {
  const reverify = (
    <Button
      variant="outline"
      size="sm"
      leadingIcon={<RefreshCw className="h-3.5 w-3.5" />}
      loading={loading}
      loadingText="Verifying…"
      onClick={onReverify}
    >
      Re-verify
    </Button>
  );

  if (error !== null) {
    return (
      <PanelFrame tone="neutral" action={reverify}>
        <p className="text-sm font-medium text-slate-800 dark:text-slate-200">
          The chain could not be verified
        </p>
        {/* Deliberately not phrased as a failure of the chain. An unreachable
            API says nothing about whether the ledger is intact, and reporting
            "broken" here would be a false accusation. */}
        <p className="text-sm text-slate-600 dark:text-slate-400">
          {error} This is a problem reaching the verification endpoint, not evidence that the ledger
          has been altered.
        </p>
      </PanelFrame>
    );
  }

  if (valid === null) {
    return (
      <PanelFrame tone="neutral" action={reverify}>
        <Skeleton className="h-5 w-72" />
        <Skeleton className="h-4 w-96" />
      </PanelFrame>
    );
  }

  if (valid) {
    return (
      <PanelFrame tone="success" action={reverify}>
        <p className="flex items-center gap-2 text-base font-semibold text-emerald-700 dark:text-emerald-400">
          <ShieldCheck className="h-5 w-5 shrink-0" aria-hidden="true" />
          Ledger verified - {eventsChecked ?? 0} {eventsChecked === 1 ? "event" : "events"}, hash
          chain intact
        </p>
        <p className="text-sm text-emerald-900/80 dark:text-emerald-200/80">
          Every hash was recomputed from genesis and matched the value stored on its entry. No row
          has been edited, removed or reordered since it was written.
        </p>
        {headHash !== null ? <HashLine label="Head hash" value={headHash} /> : null}
      </PanelFrame>
    );
  }

  return (
    <PanelFrame tone="danger" action={reverify}>
      <p className="flex items-center gap-2 text-base font-semibold text-rose-700 dark:text-rose-400">
        <ShieldAlert className="h-5 w-5 shrink-0" aria-hidden="true" />
        Ledger verification FAILED
        {brokenAtSequence !== null ? ` - chain breaks at sequence ${brokenAtSequence}` : ""}
      </p>
      <p className="text-sm text-rose-900/80 dark:text-rose-200/80">
        {message ??
          "The recomputed hash chain no longer matches what is stored. Treat every entry from the break onwards as unverified."}
      </p>
      <p className="text-sm text-rose-900/80 dark:text-rose-200/80">
        {eventsChecked ?? 0} {eventsChecked === 1 ? "entry was" : "entries were"} checked before the
        mismatch. Entries <em>before</em> the break are still verified - the chain only invalidates
        forwards.
      </p>
    </PanelFrame>
  );
}

const PANEL_TONE: Record<"success" | "danger" | "neutral", string> = {
  success: "border-emerald-200 bg-emerald-50/60 dark:border-emerald-900 dark:bg-emerald-950/30",
  danger: "border-rose-200 bg-rose-50/60 dark:border-rose-900 dark:bg-rose-950/30",
  neutral: "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-950",
};

function PanelFrame({
  tone,
  action,
  children,
}: {
  tone: "success" | "danger" | "neutral";
  action: ReactNode;
  children: ReactNode;
}) {
  return (
    <section
      // `status` rather than `alert`: the panel is re-read on demand and should
      // be announced at the next pause, not interrupt whatever is being read.
      role="status"
      className={cn("rounded-lg border p-5", PANEL_TONE[tone])}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1 space-y-2">{children}</div>
        <div className="shrink-0">{action}</div>
      </div>
    </section>
  );
}

function HashLine({ label, value }: { label: string; value: string }) {
  return (
    <p className="pt-1 text-xs">
      <span className="text-slate-500 dark:text-slate-400">{label} </span>
      <span className="break-all font-mono text-slate-700 dark:text-slate-300">{value}</span>
    </p>
  );
}

/* -------------------------------------------------------------------------- */
/* Filters                                                                     */
/* -------------------------------------------------------------------------- */

const SELECT_CLASSES =
  "h-9 rounded-lg border border-slate-200 bg-white px-2.5 text-sm text-slate-900 " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 " +
  "dark:border-slate-800 dark:bg-slate-950 dark:text-slate-100";

interface FilterBarProps {
  caseIdDraft: string;
  onCaseIdDraftChange: (value: string) => void;
  onCaseIdSubmit: () => void;
  eventTypeFilter: string;
  onEventTypeChange: (value: string) => void;
  actorTypeFilter: string;
  onActorTypeChange: (value: string) => void;
  filtersActive: boolean;
  onClear: () => void;
}

function FilterBar({
  caseIdDraft,
  onCaseIdDraftChange,
  onCaseIdSubmit,
  eventTypeFilter,
  onEventTypeChange,
  actorTypeFilter,
  onActorTypeChange,
  filtersActive,
  onClear,
}: FilterBarProps) {
  return (
    <div className="flex flex-wrap items-end gap-3">
      <label className="flex flex-col gap-1">
        <span className="text-xs font-medium text-slate-500 dark:text-slate-400">Event type</span>
        <select
          className={SELECT_CLASSES}
          value={eventTypeFilter}
          onChange={(event) => onEventTypeChange(event.target.value)}
        >
          <option value={ANY}>All event types</option>
          {EVENT_TYPE_OPTIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-xs font-medium text-slate-500 dark:text-slate-400">Actor</span>
        <select
          className={SELECT_CLASSES}
          value={actorTypeFilter}
          onChange={(event) => onActorTypeChange(event.target.value)}
        >
          <option value={ANY}>All actors</option>
          {ACTOR_TYPE_OPTIONS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>

      {/* A real <form> so Enter submits: someone pasting a case id from the
          address bar expects to press Return, not to hunt for a button. */}
      <form
        className="flex items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          onCaseIdSubmit();
        }}
      >
        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-slate-500 dark:text-slate-400">Case id</span>
          <input
            type="text"
            inputMode="text"
            spellCheck={false}
            placeholder="rc_…"
            value={caseIdDraft}
            onChange={(event) => onCaseIdDraftChange(event.target.value)}
            className={cn(SELECT_CLASSES, "w-56 font-mono text-xs placeholder:font-sans")}
          />
        </label>
        <Button type="submit" variant="secondary" size="sm" className="h-9">
          Apply
        </Button>
      </form>

      {filtersActive ? (
        <Button variant="ghost" size="sm" className="h-9" onClick={onClear}>
          Clear filters
        </Button>
      ) : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Ledger table                                                                */
/* -------------------------------------------------------------------------- */

function LedgerTable({ events }: { events: readonly AuditEvent[] }) {
  // Keyed by sequence rather than array index: the set has to survive filtering
  // and re-sorting, and an index would point at a different entry afterwards.
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(() => new Set<number>());

  const toggle = (sequence: number): void => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(sequence)) next.delete(sequence);
      else next.add(sequence);
      return next;
    });
  };

  return (
    <Table containerClassName="rounded-lg border border-slate-200 dark:border-slate-800">
      <TableHeader>
        <TableRow>
          <TableHead className="w-16 text-right">Seq</TableHead>
          <TableHead className="w-48">Recorded</TableHead>
          <TableHead className="w-52">Event</TableHead>
          <TableHead className="w-36">Actor</TableHead>
          <TableHead>Summary</TableHead>
          <TableHead className="w-40">Hash</TableHead>
          <TableHead className="w-10">
            <span className="sr-only">Payload</span>
          </TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {events.map((event) => {
          const isOpen = expanded.has(event.sequence);
          return (
            <LedgerRows key={event.id} event={event} isOpen={isOpen} onToggle={toggle} />
          );
        })}
      </TableBody>
    </Table>
  );
}

function LedgerRows({
  event,
  isOpen,
  onToggle,
}: {
  event: AuditEvent;
  isOpen: boolean;
  onToggle: (sequence: number) => void;
}) {
  const payloadEmpty = Object.keys(event.payload).length === 0;

  return (
    <>
      <TableRow>
        <TableCell className="text-right font-mono text-xs tabular-nums text-slate-500 dark:text-slate-400">
          {event.sequence}
        </TableCell>
        <TableCell className="whitespace-nowrap text-xs text-slate-600 dark:text-slate-400">
          {formatDateTime(event.created_at)}
        </TableCell>
        <TableCell>
          <Badge variant={EVENT_VARIANT[event.event_type]} dot>
            {AUDIT_EVENT_LABEL[event.event_type]}
          </Badge>
        </TableCell>
        <TableCell>
          {/* `items-start` matters: a flex column stretches its children by
              default, which would blow the badge out to the full column width
              and turn a status token into a box. */}
          <div className="flex flex-col items-start gap-0.5">
            <Badge variant={ACTOR_VARIANT[event.actor_type]}>
              {ACTOR_TYPE_LABEL[event.actor_type]}
            </Badge>
            {/* The actor id is what answers "who approved it?", so it is shown
                even when it repeats the type ("system"). */}
            <span className="truncate font-mono text-2xs text-slate-400 dark:text-slate-500">
              {event.actor_id}
            </span>
          </div>
        </TableCell>
        <TableCell className="text-sm text-slate-700 dark:text-slate-300">{event.summary}</TableCell>
        <TableCell
          className="font-mono text-2xs text-slate-500 dark:text-slate-400"
          title={event.hash}
        >
          {truncateId(event.hash, 10, 6)}
        </TableCell>
        <TableCell className="text-right">
          <button
            type="button"
            onClick={() => onToggle(event.sequence)}
            aria-expanded={isOpen}
            aria-label={`${isOpen ? "Hide" : "Show"} payload for entry ${event.sequence}`}
            className="rounded-md p-1 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700 dark:hover:bg-slate-800 dark:hover:text-slate-200"
          >
            {isOpen ? (
              <ChevronDown className="h-4 w-4" aria-hidden="true" />
            ) : (
              <ChevronRight className="h-4 w-4" aria-hidden="true" />
            )}
          </button>
        </TableCell>
      </TableRow>

      {isOpen ? (
        <TableRow className="bg-slate-50/70 dark:bg-slate-900/40">
          <TableCell colSpan={7} className="px-4 py-4">
            <div className="space-y-3">
              {/* prev_hash and hash are shown together, in that order, because
                  side by side they are the chain: this row's `prev_hash` is the
                  previous row's `hash`, and that identity is the whole
                  mechanism. Scrolling one row up and comparing the two strings
                  is a check a reviewer can do by eye. */}
              <div className="space-y-1">
                <HashRow
                  icon={<Link2 className="h-3 w-3" aria-hidden="true" />}
                  label={event.prev_hash === GENESIS_HASH ? "prev_hash (genesis)" : "prev_hash"}
                  value={event.prev_hash}
                />
                <HashRow label="hash" value={event.hash} />
              </div>

              {event.case_id !== null || event.payment_id !== null ? (
                <p className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500 dark:text-slate-400">
                  {event.case_id !== null ? (
                    <span>
                      case <span className="font-mono text-slate-700 dark:text-slate-300">{event.case_id}</span>
                    </span>
                  ) : null}
                  {event.payment_id !== null ? (
                    <span>
                      payment{" "}
                      <span className="font-mono text-slate-700 dark:text-slate-300">
                        {event.payment_id}
                      </span>
                    </span>
                  ) : null}
                </p>
              ) : null}

              {payloadEmpty ? (
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  This entry carries no payload - its summary is the whole record.
                </p>
              ) : (
                <pre className="max-h-72 overflow-auto rounded-md border border-slate-200 bg-white p-3 font-mono text-2xs leading-relaxed text-slate-700 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-300">
                  {JSON.stringify(event.payload, null, 2)}
                </pre>
              )}
            </div>
          </TableCell>
        </TableRow>
      ) : null}
    </>
  );
}

function HashRow({
  label,
  value,
  icon,
}: {
  label: string;
  value: string;
  icon?: ReactNode;
}) {
  return (
    <p className="flex flex-wrap items-baseline gap-2 text-2xs">
      <span className="inline-flex items-center gap-1 text-slate-500 dark:text-slate-400">
        {icon}
        {label}
      </span>
      <span className="break-all font-mono text-slate-700 dark:text-slate-300">{value}</span>
    </p>
  );
}
