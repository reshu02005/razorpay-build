/**
 * GuardrailChecklist - every rule the policy engine ran, and what each one said.
 *
 * ---------------------------------------------------------------------------
 * Why passing rules are rendered too
 * ---------------------------------------------------------------------------
 * The obvious design is to list only the rules that fired, because those are the
 * ones that changed the outcome. It is the wrong design here. A checklist that
 * shows three denials and nothing else leaves a reviewer to *assume* the other
 * ten checks exist, ran, and passed - and an assumption is exactly what a
 * guardrail panel is supposed to replace. "The daily budget was checked and had
 * room" is a finding. Rendering every evaluation, pass and fail alike, is what
 * turns this from a list of complaints into evidence that the control set was
 * applied in full.
 *
 * It also makes the panel self-updating: the server sends one evaluation per
 * rule, so a fourteenth rule added in Python appears here with no frontend
 * change at all. `observed` and `limit` arrive as pre-rendered display strings
 * for the same reason - the UI never learns what any individual rule measures.
 *
 * Grouped with DENY first because that is the reading order under pressure: the
 * thing that stops the case, then the thing that needs a signature, then the
 * background of checks that were satisfied.
 *
 * Nothing here decides anything. The verdict was computed on the server, is
 * re-computed there again at approval time, and is rendered - not derived - by
 * this component.
 */

import { CheckCircle2, MinusCircle, ShieldAlert, XCircle, type LucideIcon } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  GUARDRAIL_DECISION_LABEL,
  GUARDRAIL_DECISION_TONE,
  type GuardrailDecision,
  type GuardrailEvaluation,
} from '@/lib/types';

/** Most restrictive first - the reading order an operator needs under pressure. */
const DECISION_ORDER: readonly GuardrailDecision[] = ['deny', 'require_approval', 'allow'];

const DECISION_ICON: Record<GuardrailDecision, LucideIcon> = {
  deny: XCircle,
  require_approval: ShieldAlert,
  allow: CheckCircle2,
};

const DECISION_ICON_CLASS: Record<GuardrailDecision, string> = {
  deny: 'text-rose-600 dark:text-rose-400',
  require_approval: 'text-amber-600 dark:text-amber-400',
  allow: 'text-emerald-600 dark:text-emerald-400',
};

/** Section headings, phrased as what the group means rather than as its token. */
const GROUP_HEADING: Record<GuardrailDecision, string> = {
  deny: 'Blocking - these stop the recovery outright',
  require_approval: 'Require a human signature',
  allow: 'Satisfied',
};

const NOT_APPLICABLE_HEADING = 'Not consulted - no payment attempt is proposed';

export interface GuardrailChecklistProps {
  /** Every rule the engine ran, in evaluation order. */
  evaluations: readonly GuardrailEvaluation[];
  /** The aggregate verdict: most restrictive rule wins. */
  decision: GuardrailDecision;
  className?: string;
}

function EvaluationRow({ evaluation }: { evaluation: GuardrailEvaluation }) {
  // A not-applicable row is deliberately not drawn as a pass. It carries
  // `decision: 'allow'` on the wire because the rule raised no objection, but it
  // raised none only because it was never consulted, and a green tick would
  // claim otherwise.
  const isNotApplicable = !evaluation.applicable;
  const Icon = isNotApplicable ? MinusCircle : DECISION_ICON[evaluation.decision];
  const iconClass = isNotApplicable
    ? 'text-slate-300 dark:text-slate-600'
    : DECISION_ICON_CLASS[evaluation.decision];
  const hasObservation = evaluation.observed !== null || evaluation.limit !== null;

  return (
    <li className={`flex items-start gap-3 py-3 ${isNotApplicable ? 'opacity-60' : ''}`}>
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${iconClass}`} aria-hidden="true" />

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="text-sm font-medium text-slate-900 dark:text-slate-100">
            {evaluation.name}
          </span>
          {/* The rule id is shown because it is the shared vocabulary between
              this screen, the audit ledger, the policy page and the Python
              source. An operator quoting "R7" in a support thread is precise. */}
          <span className="font-mono text-2xs text-slate-400 dark:text-slate-500">
            {evaluation.rule_id}
          </span>
        </div>

        <p className="mt-0.5 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
          {evaluation.reason}
        </p>

        {hasObservation ? (
          <p className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-2xs">
            {evaluation.observed === null ? null : (
              <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-700 dark:bg-slate-800 dark:text-slate-200">
                observed {evaluation.observed}
              </span>
            )}
            {evaluation.limit === null ? null : (
              <span className="rounded bg-slate-100 px-1.5 py-0.5 text-slate-700 dark:bg-slate-800 dark:text-slate-200">
                limit {evaluation.limit}
              </span>
            )}
          </p>
        ) : null}

        <p className="mt-1 text-2xs leading-relaxed text-slate-400 dark:text-slate-500">
          {evaluation.description}
        </p>
      </div>
    </li>
  );
}

export function GuardrailChecklist({
  evaluations,
  decision,
  className,
}: GuardrailChecklistProps) {
  const consulted = evaluations.filter((evaluation) => evaluation.applicable);
  const notConsulted = evaluations.filter((evaluation) => !evaluation.applicable);

  const groups = DECISION_ORDER.map((groupDecision) => ({
    decision: groupDecision,
    items: consulted.filter((evaluation) => evaluation.decision === groupDecision),
  })).filter((group) => group.items.length > 0);

  return (
    <Card className={className}>
      <CardHeader
        action={
          <Badge variant={GUARDRAIL_DECISION_TONE[decision]} dot>
            {GUARDRAIL_DECISION_LABEL[decision]}
          </Badge>
        }
      >
        <CardTitle>Guardrails</CardTitle>
        <CardDescription>
          {evaluations.length === 0
            ? 'No evaluations were recorded for this case.'
            : notConsulted.length === evaluations.length
              ? `All ${evaluations.length} rules are listed. None were consulted: the proposed strategy creates no payment attempt, so there was nothing to constrain.`
              : `All ${evaluations.length} rules are listed, passed ones included. The most restrictive verdict wins.`}
        </CardDescription>
      </CardHeader>

      <CardContent>
        {evaluations.length === 0 ? (
          <p className="text-sm text-slate-500 dark:text-slate-400">
            No guardrail evaluations were recorded for this case.
          </p>
        ) : (
          <div className="space-y-5">
            {groups.map((group) => (
              <div key={group.decision}>
                <p className="mb-1 flex items-baseline gap-2 text-2xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                  {GROUP_HEADING[group.decision]}
                  <span className="font-mono normal-case tracking-normal text-slate-400 dark:text-slate-500">
                    {group.items.length}
                  </span>
                </p>
                <ul className="divide-y divide-slate-100 dark:divide-slate-800/70">
                  {group.items.map((evaluation) => (
                    <EvaluationRow key={evaluation.rule_id} evaluation={evaluation} />
                  ))}
                </ul>
              </div>
            ))}

            {/* Listed last and greyed out, but still listed. Hiding them would
                leave an operator unable to tell "the engine was not consulted"
                apart from "the engine has no rules", and the whole point of
                rendering the full checklist is that absence is visible. */}
            {notConsulted.length === 0 ? null : (
              <div>
                <p className="mb-1 flex items-baseline gap-2 text-2xs font-medium uppercase tracking-wide text-slate-400 dark:text-slate-500">
                  {NOT_APPLICABLE_HEADING}
                  <span className="font-mono normal-case tracking-normal text-slate-400 dark:text-slate-500">
                    {notConsulted.length}
                  </span>
                </p>
                <ul className="divide-y divide-slate-100 dark:divide-slate-800/70">
                  {notConsulted.map((evaluation) => (
                    <EvaluationRow key={evaluation.rule_id} evaluation={evaluation} />
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
