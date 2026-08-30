'use client';

/**
 * AgentTrace - every tool the agent actually executed, in order, with its
 * arguments and its result.
 *
 * ---------------------------------------------------------------------------
 * Why every step carries a capability badge
 * ---------------------------------------------------------------------------
 * The central safety claim of this system is that the agent has no tool that
 * moves money: order creation is not gated behind a check, it is absent from
 * the toolset entirely. A README can assert that. A reviewer cannot verify an
 * assertion.
 *
 * Every tool carries a `ToolCapability` that is persisted with the trace row, so
 * this panel can show, per executed step, what class of thing that step was
 * allowed to do - and the summary line at the top counts the `financial` steps
 * across the whole run. That count is read out of the recorded data, not out of
 * a constant in this file, which is what makes it evidence rather than a second
 * claim. If a financial tool were ever registered and called, this panel would
 * say so in rose, on the case that used it.
 *
 * The steps are collapsed by default and expand to raw JSON. Collapsed, the
 * trace reads as a narrative an operator can skim in seconds; expanded, it is
 * the unedited payload, because "trust the summary" is not what an explainability
 * panel is for. The JSON is rendered with `JSON.stringify(value, null, 2)` and
 * scrolls inside its own container so a long array can never widen the page.
 *
 * The rule-based planner records synthetic steps for exactly this reason: a
 * degraded run still produces a readable trace instead of an empty panel that
 * looks like a bug.
 */

import { useMemo, useState } from 'react';
import { ChevronDown, ChevronRight, CircleAlert, ShieldCheck, Wrench } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import {
  TOOL_CAPABILITY_LABEL,
  TOOL_CAPABILITY_TONE,
  type AgentToolCall,
} from '@/lib/types';
import { cn } from '@/lib/utils';

export interface AgentTraceProps {
  steps: readonly AgentToolCall[];
  className?: string;
}

/** Renders a free-form JSON payload without ever indexing into it by name. */
function JsonBlock({ label, value }: { label: string; value: Record<string, unknown> }) {
  const isEmpty = Object.keys(value).length === 0;

  return (
    <div className="min-w-0">
      <p className="mb-1 text-2xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {label}
      </p>
      {isEmpty ? (
        <p className="rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1.5 font-mono text-2xs text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
          {'{}'}
        </p>
      ) : (
        <pre className="overflow-x-auto rounded-md border border-slate-200 bg-slate-50 p-3 font-mono text-2xs leading-relaxed text-slate-800 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-200">
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </div>
  );
}

export function AgentTrace({ steps, className }: AgentTraceProps) {
  // A set of ids rather than a single "expanded step": comparing the arguments
  // of step 2 against the result of step 5 is the normal way to audit a trace,
  // and an accordion that closes the previous panel makes that impossible.
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set<string>());

  const summary = useMemo(() => {
    let readOnly = 0;
    let writeProposal = 0;
    let financial = 0;
    let totalLatencyMs = 0;

    for (const step of steps) {
      if (step.capability === 'read_only') readOnly += 1;
      if (step.capability === 'write_proposal') writeProposal += 1;
      if (step.capability === 'financial') financial += 1;
      totalLatencyMs += step.latency_ms;
    }

    return { readOnly, writeProposal, financial, totalLatencyMs };
  }, [steps]);

  const toggle = (id: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <Card className={className}>
      <CardHeader
        action={
          steps.length === 0 ? null : (
            <span className="font-mono text-2xs text-slate-500 dark:text-slate-400">
              {summary.totalLatencyMs} ms total
            </span>
          )
        }
      >
        <CardTitle className="flex items-center gap-2">
          <Wrench className="h-4 w-4 text-slate-500 dark:text-slate-400" aria-hidden="true" />
          Agent trace
        </CardTitle>
        <CardDescription>
          Every tool call the agent executed while producing its recommendation, with the
          arguments it passed and the result it received.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        {steps.length === 0 ? (
          <EmptyState
            size="sm"
            icon={<Wrench className="h-5 w-5" aria-hidden="true" />}
            title="No trace was recorded for this case"
            description="Every analysis writes one row per step, including the rule-based path. An empty trace means the case predates tracing or the run failed before its first tool call."
          />
        ) : (
          <>
            {/*
             * The capability tally, computed from the recorded rows. The
             * financial count is the load-bearing one: it is the safety property
             * of the whole design, stated as a number a reviewer can check
             * against the expanded steps below rather than as a claim.
             */}
            <div
              className={cn(
                'flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border px-3 py-2.5 text-xs',
                summary.financial === 0
                  ? 'border-emerald-200 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/50 dark:text-emerald-200'
                  : 'border-rose-200 bg-rose-50 text-rose-900 dark:border-rose-900 dark:bg-rose-950/50 dark:text-rose-200',
              )}
            >
              {summary.financial === 0 ? (
                <ShieldCheck className="h-4 w-4 shrink-0" aria-hidden="true" />
              ) : (
                <CircleAlert className="h-4 w-4 shrink-0" aria-hidden="true" />
              )}
              <span>
                <strong className="font-medium">{summary.financial}</strong> money-moving tool
                calls in <strong className="font-medium">{steps.length}</strong> steps
              </span>
              <span className="text-slate-600 dark:text-slate-400">
                {summary.readOnly} read-only &middot; {summary.writeProposal} proposal
              </span>
            </div>

            <ol className="space-y-2">
              {steps.map((step) => {
                const isOpen = expanded.has(step.id);
                const Chevron = isOpen ? ChevronDown : ChevronRight;

                return (
                  <li
                    key={step.id}
                    className="overflow-hidden rounded-lg border border-slate-200 dark:border-slate-800"
                  >
                    <button
                      type="button"
                      onClick={() => toggle(step.id)}
                      aria-expanded={isOpen}
                      className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-slate-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-slate-400 dark:hover:bg-slate-900"
                    >
                      <Chevron
                        className="h-4 w-4 shrink-0 text-slate-400 dark:text-slate-500"
                        aria-hidden="true"
                      />

                      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-slate-100 font-mono text-2xs text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                        {step.step}
                      </span>

                      <span className="min-w-0 flex-1 truncate font-mono text-xs text-slate-900 dark:text-slate-100">
                        {step.tool_name}
                      </span>

                      <Badge variant={TOOL_CAPABILITY_TONE[step.capability]}>
                        {TOOL_CAPABILITY_LABEL[step.capability]}
                      </Badge>

                      {step.ok ? null : (
                        <Badge variant="danger" dot>
                          Errored
                        </Badge>
                      )}

                      <span className="shrink-0 font-mono text-2xs tabular-nums text-slate-400 dark:text-slate-500">
                        {step.latency_ms} ms
                      </span>
                    </button>

                    {isOpen ? (
                      <div className="space-y-3 border-t border-slate-200 bg-white px-3 py-3 dark:border-slate-800 dark:bg-slate-950">
                        {step.error === null ? null : (
                          <p className="rounded-md border border-rose-200 bg-rose-50 px-2.5 py-1.5 text-xs text-rose-900 dark:border-rose-900 dark:bg-rose-950/60 dark:text-rose-200">
                            {step.error}
                          </p>
                        )}
                        <JsonBlock label="Arguments" value={step.arguments} />
                        <JsonBlock label="Result" value={step.result} />
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ol>
          </>
        )}
      </CardContent>
    </Card>
  );
}
