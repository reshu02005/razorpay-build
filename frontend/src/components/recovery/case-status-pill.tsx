/**
 * CaseStatusPill - one recovery case's state, rendered the same way everywhere.
 *
 * The recovery state machine has twelve members and six of them are terminal.
 * Under time pressure an operator has to separate "a guardrail refused this"
 * from "the agent judged that nothing should be done" from "the money arrived"
 * in one glance, so the label and the colour both come from the shared maps in
 * `@/lib/types` rather than being chosen here. Those maps are typed
 * `Record<RecoveryStatus, ...>`, so a new status on the Python side cannot reach
 * a screen until somebody has written its copy - the compiler refuses to build
 * a pill that would render a raw token like `awaiting_payment` at an operator.
 *
 * Deliberately nothing but a lookup. Every question of the form "what may the
 * operator do in this state?" is answered on the server and arrives as
 * `can_approve` / `can_reject`. A status pill that started reasoning about the
 * state machine would be the first step towards a second, divergent copy of it.
 */

import {
  Ban,
  CircleAlert,
  CircleCheck,
  Clock,
  Hourglass,
  Info,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  UserCheck,
  XCircle,
  type LucideIcon,
} from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import {
  RECOVERY_STATUS_LABEL,
  RECOVERY_STATUS_TONE,
  type RecoveryStatus,
} from '@/lib/types';
import { cn } from '@/lib/utils';

/**
 * One icon per state. The icon carries the same meaning as the colour, which is
 * the point: roughly one in twelve men cannot separate the emerald "recovered"
 * chip from the rose "failed" one, and a console about money should not encode
 * that distinction in hue alone.
 */
const STATUS_ICON: Record<RecoveryStatus, LucideIcon> = {
  proposed: Sparkles,
  blocked: ShieldAlert,
  awaiting_approval: Clock,
  rejected: XCircle,
  approved: ShieldCheck,
  executing: RefreshCw,
  awaiting_payment: Hourglass,
  recovered: CircleCheck,
  failed: CircleAlert,
  expired: Ban,
  no_action: Info,
  escalated: UserCheck,
};

export interface CaseStatusPillProps {
  status: RecoveryStatus;
  /** `lg` is for a screen heading, where the pill is the primary answer. */
  size?: 'sm' | 'lg';
  className?: string;
}

export function CaseStatusPill({ status, size = 'sm', className }: CaseStatusPillProps) {
  const Icon = STATUS_ICON[status];

  return (
    <Badge
      variant={RECOVERY_STATUS_TONE[status]}
      icon={<Icon className={size === 'lg' ? 'h-4 w-4' : 'h-3.5 w-3.5'} aria-hidden="true" />}
      className={cn(size === 'lg' && 'gap-2 px-2.5 py-1 text-sm', className)}
    >
      {RECOVERY_STATUS_LABEL[status]}
    </Badge>
  );
}
