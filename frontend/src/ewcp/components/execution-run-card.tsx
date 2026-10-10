"use client";

// ExecutionRunCard — one ExecutionRunMap row in the run list: intent,
// lifecycle status (WP-A6 projection — kernel truth when the A6-07
// `workrun_status` projection is bound, launcher status otherwise), a
// pending-decision chip when the kernel reports an open human gate,
// task mode, governed link when bound. Click selects the run's detail
// surface.

import type { ExecutionRun } from "@/ewcp/api";
import { lifecycleStatus, taskModeLabel } from "@/ewcp/labels";

import { LifecycleBadge } from "./lifecycle-badge";
import { UnverifiedClaimsChip } from "./unverified-claims-chip";

export function ExecutionRunCard({
  run,
  active,
  onSelect,
}: {
  run: ExecutionRun;
  active: boolean;
  onSelect: (run: ExecutionRun) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(run)}
      className={`w-full rounded-lg border p-3 text-left transition-colors ${
        active
          ? "border-primary bg-accent"
          : "border-border bg-card hover:bg-accent/60"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1">
          <LifecycleBadge status={lifecycleStatus(run, run.workrun_status)} />
          {run.workrun_status?.pending_decision && (
            <span
              title={`Kernel event gần nhất: ${new Date(
                run.workrun_status.last_event_at * 1000,
              ).toLocaleString()}`}
              className="rounded border border-amber-500/50 bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700 dark:bg-amber-950/40 dark:text-amber-300"
            >
              Chờ quyết định
            </span>
          )}
          <UnverifiedClaimsChip integrityFlag={run.integrity_flag} />
        </span>
        <span className="text-muted-foreground text-[10px]">
          {taskModeLabel(run.task_mode)}
        </span>
      </div>
      <p className="mt-1 line-clamp-2 text-sm">{run.intent}</p>
      <div className="text-muted-foreground mt-1 flex items-center gap-2 font-mono text-[10px]">
        <span title="execution_run_id">
          {run.execution_run_id.slice(0, 8)}…
        </span>
        {run.workrun_id && (
          <span title="workrun_id">wr:{run.workrun_id.slice(0, 8)}…</span>
        )}
      </div>
    </button>
  );
}
