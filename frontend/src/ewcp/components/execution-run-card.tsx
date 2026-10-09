"use client";

// ExecutionRunCard — one ExecutionRunMap row in the run list: intent,
// lifecycle status (WP-A6 projection — kernel truth when the A6-07
// `workrun_status` projection is bound, launcher status otherwise), a
// pending-decision chip when the kernel reports an open human gate,
// task mode, governed link when bound. Click selects the run's detail
// surface.

import type { ExecutionRun } from "@/ewcp/api";
import { lifecycleStatus } from "@/ewcp/labels";

import { LifecycleBadge } from "./lifecycle-badge";

const MISSING_CLAIMS_PREFIX = "claimed_artifacts_missing:";

/** F2 guardrail surface — decode the advisory flag back to the claimed
 * paths that did not verify, for the chip tooltip. */
function missingClaims(flag: string | null | undefined): string[] | null {
  if (!flag?.startsWith(MISSING_CLAIMS_PREFIX)) return null;
  try {
    const parsed = JSON.parse(flag.slice(MISSING_CLAIMS_PREFIX.length));
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function ExecutionRunCard({
  run,
  active,
  onSelect,
}: {
  run: ExecutionRun;
  active: boolean;
  onSelect: (run: ExecutionRun) => void;
}) {
  const missing = missingClaims(run.integrity_flag);
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
          {missing !== null && (
            <span
              title={
                missing.length > 0
                  ? `Không tìm thấy: ${missing.join(", ")}`
                  : "Kết quả tự báo cáo không xác minh được trên đĩa"
              }
              className="rounded border border-red-500/50 bg-red-50 px-1.5 py-0.5 text-[10px] font-semibold text-red-700 dark:bg-red-950/40 dark:text-red-300"
            >
              Tự báo cáo — chưa xác minh
            </span>
          )}
        </span>
        <span className="text-muted-foreground text-[10px]">
          {run.task_mode === "governed"
            ? "governed"
            : run.task_mode === "invoke"
              ? "invoke"
              : "general"}
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
