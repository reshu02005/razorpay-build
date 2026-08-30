/**
 * AiVerdictCard - what the agent concluded, and which engine concluded it.
 *
 * This is the recommendation half of the screen: a classification of the
 * failure, a confidence in that classification, a recommended strategy, and the
 * rationale in the agent's own words. It is tinted with the `ai` role - the one
 * colour in this console reserved for machine output - so that a reviewer can
 * see at a glance which parts of the page are inference and which are record.
 *
 * ---------------------------------------------------------------------------
 * Why the engine badge is a correctness requirement, not a flourish
 * ---------------------------------------------------------------------------
 * The same `RecoveryCase` shape comes back whether Google Gemini produced the
 * plan through a function-calling loop or the deterministic rule planner did,
 * because the system is built to run with zero credentials. Those two engines
 * have genuinely different failure modes: a model can be confidently wrong on a
 * failure it has never seen, and a lookup table cannot be wrong in that way but
 * also cannot notice anything its author did not anticipate. An operator about
 * to approve money movement is entitled to know which one is talking to them,
 * and a card that rendered the rationale without naming its author would be
 * hiding the single most useful fact about how much to trust it.
 *
 * The confidence is rendered as a labelled value ("Classification confidence
 * 82%") rather than as a bare number floating next to the category. A naked
 * "0.82" invites the reader to attach it to whatever is nearest - very often
 * the strategy, which is not what it measures at all.
 */

import { ArrowRight, ListChecks, Sparkles } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { formatConfidence } from '@/lib/format';
import {
  AGENT_MODE_LABEL,
  FAILURE_CATEGORY_LABEL,
  RECOVERY_STRATEGY_LABEL,
  type AgentMode,
  type FailureCategory,
  type RecoveryStrategy,
} from '@/lib/types';

export interface AiVerdictCardProps {
  failureCategory: FailureCategory;
  /** 0..1 - confidence in the *classification*, not in the strategy. */
  confidence: number;
  strategy: RecoveryStrategy;
  /** Shown verbatim: the human is judging the real reasoning, not a summary. */
  rationale: string;
  /** What the customer would be told. Different audience, different tone. */
  customerMessage: string;
  agentMode: AgentMode;
  className?: string;
}

export function AiVerdictCard({
  failureCategory,
  confidence,
  strategy,
  rationale,
  customerMessage,
  agentMode,
  className,
}: AiVerdictCardProps) {
  const llm = agentMode === 'llm';

  return (
    <Card className={className}>
      <CardHeader
        action={
          // The engine badge sits in the header, level with the title, rather
          // than in a footnote: it qualifies everything in the card, so it has
          // to be read before the rationale, not after it.
          <Badge
            variant={llm ? 'ai' : 'neutral'}
            icon={
              llm ? (
                <Sparkles className="h-3.5 w-3.5" aria-hidden="true" />
              ) : (
                <ListChecks className="h-3.5 w-3.5" aria-hidden="true" />
              )
            }
          >
            {AGENT_MODE_LABEL[agentMode]}
          </Badge>
        }
      >
        <CardTitle className="flex items-center gap-2 text-sky-700 dark:text-sky-300">
          <Sparkles className="h-4 w-4" aria-hidden="true" />
          Agent recommendation
        </CardTitle>
      </CardHeader>

      <CardContent className="space-y-5">
        {/* Classification -> strategy, read left to right. The arrow is the
            whole thesis of the product in one glyph: the recovery action is
            chosen *because of* the failure reason, not in spite of it. */}
        <div className="rounded-lg border border-sky-200 bg-sky-50/70 p-4 dark:border-sky-900 dark:bg-sky-950/40">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <div className="min-w-0">
              <p className="text-2xs uppercase tracking-wide text-sky-700/80 dark:text-sky-300/80">
                Classified as
              </p>
              <p className="mt-0.5 font-display text-base font-semibold text-slate-900 dark:text-slate-50">
                {FAILURE_CATEGORY_LABEL[failureCategory]}
              </p>
            </div>

            <ArrowRight
              className="mt-4 hidden h-4 w-4 shrink-0 text-sky-600 dark:text-sky-400 sm:block"
              aria-hidden="true"
            />

            <div className="min-w-0">
              <p className="text-2xs uppercase tracking-wide text-sky-700/80 dark:text-sky-300/80">
                Recommended strategy
              </p>
              <p className="mt-0.5 font-display text-base font-semibold text-slate-900 dark:text-slate-50">
                {RECOVERY_STRATEGY_LABEL[strategy]}
              </p>
            </div>
          </div>

          <p className="mt-3 text-xs text-sky-800/90 dark:text-sky-200/90">
            Classification confidence{' '}
            <span className="font-mono font-semibold">{formatConfidence(confidence)}</span> - how
            sure the agent is of the failure category above, not of the strategy.
          </p>
        </div>

        <div>
          <p className="mb-1.5 text-2xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Rationale, in the agent&apos;s own words
          </p>
          {/* `whitespace-pre-line` preserves the paragraph breaks the engine
              actually produced. Reflowing them would be a small edit to text
              the operator is being asked to judge as written. */}
          <p className="whitespace-pre-line text-sm leading-relaxed text-slate-700 dark:text-slate-200">
            {rationale}
          </p>
        </div>

        <Separator />

        <div>
          <p className="mb-1.5 text-2xs uppercase tracking-wide text-slate-500 dark:text-slate-400">
            What the customer would be told
          </p>
          <blockquote className="border-l-2 border-slate-200 pl-3 text-sm italic leading-relaxed text-slate-600 dark:border-slate-700 dark:text-slate-300">
            {customerMessage}
          </blockquote>
        </div>
      </CardContent>
    </Card>
  );
}
