"use client";

// ExecutionRunCard — one ExecutionRunMap row in the run list: intent,
// lifecycle status (launcher projection — pending_interrupt is NOT
// complete), task mode, governed link when bound. Click selects the
// run's detail surface.

import type { ExecutionRun } from "@/ewcp/api";
import { statusLabel } from "@/ewcp/labels";

const STATUS_CLS: Record<string, string> = {
  launching: "bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300",
  running: "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300",
  pending_interrupt:
    "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  completed:
    "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
  failed: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  timeout: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  interrupted:
    "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
};

export function runStatusLabel(status: ExecutionRun["status"]): string {
  switch (status) {
    case "launching":
      return "Đang khởi tạo";
    case "running":
      return "Đang chạy";
    case "pending_interrupt":
      return "Chờ thao tác";
    case "completed":
      return "Hoàn tất";
    case "failed":
      return "Lỗi";
    case "timeout":
      return "Hết giờ";
    case "interrupted":
      return "Đã dừng";
    default:
      return statusLabel(status);
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
  return (
    <button
      type="button"
      onClick={() => onSelect(run)}
      className={`w-full rounded-lg border p-3 text-left transition-colors ${
        active
          ? "border-blue-500 bg-blue-50 dark:border-blue-600 dark:bg-blue-950/30"
          : "border-zinc-300 bg-white hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:hover:bg-zinc-800"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-semibold ${STATUS_CLS[run.status] ?? STATUS_CLS.launching}`}
        >
          {runStatusLabel(run.status)}
        </span>
        <span className="text-[10px] text-zinc-400">
          {run.task_mode === "governed" ? "governed" : "general"}
        </span>
      </div>
      <p className="mt-1 line-clamp-2 text-sm">{run.intent}</p>
      <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-zinc-400">
        <span title="execution_run_id">
          {run.execution_run_id.slice(0, 8)}…
        </span>
        {run.workrun_id && <span title="workrun_id">wr:{run.workrun_id.slice(0, 8)}…</span>}
      </div>
    </button>
  );
}
