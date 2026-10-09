"use client";

// ExecutionRunCard — one ExecutionRunMap row in the run list: intent,
// lifecycle status (the unified WP-A6 projection — the list endpoint
// carries no workrun view, so the launcher record projects it), task
// mode, governed link when bound. Click selects the run's detail
// surface.

import type { ExecutionRun } from "@/ewcp/api";
import { lifecycleStatus } from "@/ewcp/labels";

import { LifecycleBadge } from "./lifecycle-badge";

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
        <LifecycleBadge status={lifecycleStatus(run)} />
        <span className="text-[10px] text-muted-foreground">
          {run.task_mode === "governed" ? "governed" : "general"}
        </span>
      </div>
      <p className="mt-1 line-clamp-2 text-sm">{run.intent}</p>
      <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-muted-foreground">
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
