'use client';

/**
 * AuditTimeline - the case's entries in the hash-chained ledger, oldest first.
 *
 * ---------------------------------------------------------------------------
 * Why the hash is on screen
 * ---------------------------------------------------------------------------
 * A ledger you cannot inspect is just a log. Every event carries the SHA-256 of
 * its own canonical content plus the hash of the event before it, which is what
 * makes a silent edit to history detectable: change one summary and every hash
 * after it stops matching. That property is worth nothing to a reviewer who
 * cannot see the hashes, so a truncated head-and-tail of each one is rendered
 * inline, with the full value in the element's `title` for copying, and the
 * `/audit` screen re-computes the whole chain from genesis on demand.
 *
 * The truncation keeps both ends of the digest rather than a prefix: matching a
 * hash by eye against another screen is a head-and-tail comparison, and a
 * leading substring alone is the half that collides most convincingly.
 *
 * ---------------------------------------------------------------------------
 * Why the actor is an icon and a name
 * ---------------------------------------------------------------------------
 * "Who approved it?" is the question this timeline exists to answer. The agent,
 * a human operator, a scheduled system sweep and a gateway webhook are four
 * genuinely different kinds of authorship, and reading a name alone ("system")
 * does not tell you which. The icon carries the class; the `actor_id` carries
 * the identity.
 *
 * Client component: relative timestamps read the wall clock, so they differ
 * between a server render and hydration. Keeping this on the client side of the
 * boundary is what stops that being a hydration mismatch on every reload.
 */

import { Bot, Server, ScrollText, User, Webhook, type LucideIcon } from 'lucide-react';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { EmptyState } from '@/components/ui/empty-state';
import { formatDateTime, formatRelativeTime, truncateId } from '@/lib/format';
import {
  ACTOR_TYPE_LABEL,
  AUDIT_EVENT_LABEL,
  type ActorType,
  type AuditEvent,
  type AuditEventType,
  type Tone,
} from '@/lib/types';
import { TONE_DOT_CLASSES } from '@/lib/utils';

const ACTOR_ICON: Record<ActorType, LucideIcon> = {
  agent: Bot,
  human: User,
  system: Server,
  webhook: Webhook,
};

/**
 * Colour is reserved for the handful of entries that changed what the system was
 * allowed to do. Everything else is neutral: forty coloured dots down a rail
 * would make the four that matter unfindable.
 */
const EVENT_TONE: Record<AuditEventType, Tone> = {
  payment_failed: 'danger',
  analysis_started: 'neutral',
  failure_classified: 'info',
  propensity_scored: 'neutral',
  strategy_proposed: 'info',
  guardrails_evaluated: 'neutral',
  recovery_blocked: 'danger',
  approval_requested: 'warning',
  approval_granted: 'success',
  approval_rejected: 'danger',
  recovery_order_created: 'info',
  recovery_link_sent: 'neutral',
  payment_verified: 'success',
  recovery_succeeded: 'success',
  recovery_failed: 'danger',
  recovery_expired: 'neutral',
  webhook_received: 'neutral',
  agent_degraded: 'warning',
  gateway_simulated: 'warning',
};

export interface AuditTimelineProps {
  events: readonly AuditEvent[];
  className?: string;
}

export function AuditTimeline({ events, className }: AuditTimelineProps) {
  // Oldest first: this is a narrative of how the case got where it is, and a
  // narrative read backwards is a list. The copy before `sort()` matters -
  // `sort()` mutates, and re-ordering a prop as a side effect of rendering it
  // would quietly reorder the caller's data too.
  const ordered = [...events].sort((a, b) => a.sequence - b.sequence);

  return (
    <Card className={className}>
      <CardHeader
        action={
          ordered.length === 0 ? null : (
            <span className="font-mono text-2xs text-slate-500 dark:text-slate-400">
              {ordered.length} entries
            </span>
          )
        }
      >
        <CardTitle className="flex items-center gap-2">
          <ScrollText className="h-4 w-4 text-slate-500 dark:text-slate-400" aria-hidden="true" />
          Audit trail
        </CardTitle>
        <CardDescription>
          Append-only and hash-chained. Each entry commits to the one before it.
        </CardDescription>
      </CardHeader>

      <CardContent>
        {ordered.length === 0 ? (
          <EmptyState
            size="sm"
            icon={<ScrollText className="h-5 w-5" aria-hidden="true" />}
            title="No ledger entries for this case yet"
            description="Entries are written as the case moves - classification, guardrail evaluation, approval, order creation."
          />
        ) : (
          <ol className="relative space-y-5">
            {/* The rail is one absolutely positioned line behind the markers
                rather than a border on each item, so it is continuous through
                the gaps instead of restarting at every entry. */}
            <span
              aria-hidden="true"
              className="absolute bottom-2 left-[7px] top-2 w-px bg-slate-200 dark:bg-slate-800"
            />

            {ordered.map((event) => {
              const ActorIcon = ACTOR_ICON[event.actor_type];

              return (
                <li key={event.id} className="relative flex gap-3 pl-0">
                  <span
                    aria-hidden="true"
                    className={`relative z-10 mt-1 h-[15px] w-[15px] shrink-0 rounded-full border-2 border-white dark:border-slate-950 ${TONE_DOT_CLASSES[EVENT_TONE[event.event_type]]}`}
                  />

                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      <span className="text-sm font-medium text-slate-900 dark:text-slate-100">
                        {AUDIT_EVENT_LABEL[event.event_type]}
                      </span>
                      <span
                        className="text-2xs text-slate-400 dark:text-slate-500"
                        title={formatDateTime(event.created_at)}
                      >
                        {formatRelativeTime(event.created_at)}
                      </span>
                    </div>

                    <p className="mt-0.5 text-xs leading-relaxed text-slate-600 dark:text-slate-300">
                      {event.summary}
                    </p>

                    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-slate-400 dark:text-slate-500">
                      <span className="inline-flex items-center gap-1">
                        <ActorIcon className="h-3 w-3" aria-hidden="true" />
                        {ACTOR_TYPE_LABEL[event.actor_type]}
                        <span className="font-mono">{event.actor_id}</span>
                      </span>

                      <span className="font-mono">#{event.sequence}</span>

                      <span className="font-mono" title={event.hash}>
                        {truncateId(event.hash, 8, 6)}
                      </span>
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}
