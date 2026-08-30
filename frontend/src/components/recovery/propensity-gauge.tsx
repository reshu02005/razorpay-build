/**
 * PropensityGauge - the ML model's estimate that this recovery attempt succeeds.
 *
 * One number, three pieces of context that stop it being misread:
 *
 * 1.  The **model version**, because "the model said 0.71" is unfalsifiable
 *     without knowing which model. Two versions can disagree on the same case
 *     and both be doing their job.
 * 2.  The **top factors**, because a score with no drivers cannot be argued
 *     with, and an operator who cannot argue with a score will either rubber
 *     stamp it or ignore it. Neither is oversight.
 * 3.  A loud **"heuristic estimate"** marker when `propensity_is_fallback` is
 *     set. The application is built to run with no trained artefact present, in
 *     which case a documented heuristic produces the number instead. A heuristic
 *     dressed up as a model prediction is the kind of thing that quietly erodes
 *     trust in every other number on the screen, so it is labelled rather than
 *     smoothed over.
 *
 * The tone thresholds (>=0.6 success, >=0.3 warning, below that danger) are a
 * *reading aid*, not a decision. The number that actually gates anything is
 * `min_propensity_score` in the policy, enforced server-side by rule
 * R10_PROPENSITY_FLOOR and shown in the guardrail checklist. This component
 * never says whether a score is acceptable - only how it looks.
 */

import { Gauge, TriangleAlert } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Progress, type ProgressTone } from '@/components/ui/progress';
import { formatConfidence } from '@/lib/format';
import type { AgentToolCall } from '@/lib/types';

/** Colour thresholds for reading the bar at a glance. Not a policy boundary. */
const SUCCESS_THRESHOLD = 0.6;
const WARNING_THRESHOLD = 0.3;

/** The trace step whose recorded result carries the score's drivers. */
const PROPENSITY_TOOL_NAME = 'score_recovery_propensity';

function toneForScore(score: number): ProgressTone {
  if (score >= SUCCESS_THRESHOLD) return 'success';
  if (score >= WARNING_THRESHOLD) return 'warning';
  return 'danger';
}

/**
 * Recovers the score's drivers from the recorded agent trace.
 *
 * `RecoveryCaseOut` carries the score, its model version and its fallback flag,
 * but not the factor list - the case row stores a decision, not a full model
 * output. The factors were, however, written into the trace when the agent
 * called `score_recovery_propensity`, and that row is immutable. Reading them
 * back from the trace therefore shows the operator the drivers that were
 * actually recorded at scoring time rather than drivers recomputed later
 * against different data.
 *
 * `result` is typed `Record<string, unknown>` because it is free-form JSON, so
 * every step of the walk down to a `string[]` is checked. A malformed or absent
 * entry yields an empty list, and an empty list renders as an honest absence -
 * never as an invented factor.
 */
export function topFactorsFromTrace(trace: readonly AgentToolCall[]): string[] {
  // Last write wins: the agent may score more than one candidate strategy, and
  // the final call is the one behind the strategy it went on to propose. The
  // copy before `reverse()` matters - `reverse()` mutates, and mutating a prop
  // would reorder the caller's trace as a side effect of reading it.
  const scored = [...trace]
    .reverse()
    .find((step) => step.tool_name === PROPENSITY_TOOL_NAME);

  if (scored === undefined) return [];

  const factors = scored.result['top_factors'];
  if (!Array.isArray(factors)) return [];

  return factors.filter((factor): factor is string => typeof factor === 'string');
}

export interface PropensityGaugeProps {
  /** 0..1 - P(this recovery attempt succeeds). */
  score: number;
  modelVersion: string;
  /** True when the trained artefact was absent and the heuristic produced this. */
  isFallback: boolean;
  topFactors: readonly string[];
  className?: string;
}

export function PropensityGauge({
  score,
  modelVersion,
  isFallback,
  topFactors,
  className,
}: PropensityGaugeProps) {
  const tone = toneForScore(score);

  return (
    <Card className={className}>
      <CardHeader
        action={
          isFallback ? (
            <Badge
              variant="warning"
              icon={<TriangleAlert className="h-3.5 w-3.5" aria-hidden="true" />}
            >
              Heuristic estimate
            </Badge>
          ) : (
            <Badge variant="neutral">
              <span className="font-mono">{modelVersion}</span>
            </Badge>
          )
        }
      >
        <CardTitle className="flex items-center gap-2">
          <Gauge className="h-4 w-4 text-slate-500 dark:text-slate-400" aria-hidden="true" />
          Recovery propensity
        </CardTitle>
        <CardDescription>
          Estimated probability that this attempt is completed by the customer.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        <div className="flex items-end justify-between gap-4">
          <span className="font-display text-3xl font-semibold tracking-tight tabular-nums text-slate-900 dark:text-slate-50">
            {formatConfidence(score)}
          </span>
          <span className="pb-1 text-xs text-slate-500 dark:text-slate-400">
            {isFallback ? 'heuristic' : 'model'}{' '}
            <span className="font-mono">{modelVersion || 'unversioned'}</span>
          </span>
        </div>

        {/* `max={1}` because the score is a 0..1 fraction, not a percentage.
            Passing the raw score against the default max of 100 would paint a
            0.71 as a bar that is 0.71% full. */}
        <Progress
          value={score}
          max={1}
          tone={tone}
          size="lg"
          label="Recovery propensity"
          valueText={`${formatConfidence(score)} likely to succeed`}
        />

        {isFallback ? (
          <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-900 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-200">
            <strong className="font-medium">Model not trained.</strong> No model artefact was
            found, so this number came from the documented heuristic fallback. Treat it as an
            ordering hint between cases, not as a calibrated probability.
          </p>
        ) : null}

        <div>
          <p className="mb-2 text-2xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Top factors
          </p>
          {topFactors.length === 0 ? (
            <p className="text-xs text-slate-500 dark:text-slate-400">
              No factor list was recorded for this score.
            </p>
          ) : (
            <ul className="space-y-1.5">
              {topFactors.map((factor) => (
                <li
                  key={factor}
                  className="flex items-start gap-2 text-xs leading-relaxed text-slate-600 dark:text-slate-300"
                >
                  <span
                    aria-hidden="true"
                    className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-slate-400 dark:bg-slate-500"
                  />
                  {factor}
                </li>
              ))}
            </ul>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
