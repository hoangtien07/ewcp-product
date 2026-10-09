"use client";

// LifecycleBadge — the ONE status pill every EWCP surface renders:
// the WP-A6 lifecycle token + its Vietnamese label. Replaces the old
// per-component status pills (launcher enum on the card, kernel enum on
// the thread) so the same run reads the same words everywhere.

import { Badge } from "@/components/ui/badge";
import { LIFECYCLE_LABEL, type LifecycleStatus } from "@/ewcp/labels";

// Lifecycle semantics → app color story: blue = in flight, emerald =
// done/sealed, violet = write path, amber = ambiguous/unclaimable.
const LIFECYCLE_CLS: Record<LifecycleStatus, string> = {
  accepted:
    "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300",
  agent_finished:
    "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
  verified: "bg-emerald-600 text-white dark:bg-emerald-600",
  approved_to_act:
    "bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300",
  external_executed: "bg-violet-600 text-white dark:bg-violet-600",
  UNKNOWN:
    "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
};

export function LifecycleBadge({ status }: { status: LifecycleStatus }) {
  return (
    <Badge
      variant="secondary"
      title={status}
      className={`text-[10px] font-semibold ${LIFECYCLE_CLS[status]}`}
    >
      {LIFECYCLE_LABEL[status]}
    </Badge>
  );
}
