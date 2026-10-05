"use client";

// TaskList — the governed workbench's history rail. Lists the tenant's runs
// (GET /workruns?tenant_id=) so an earlier ask-back, approval, or sealed run
// is reachable after reload/navigation — the workspace is not single-shot.

import { useEffect, useState } from "react";

import { listRuns, type RunView } from "@/ewcp/api";

interface Creds {
  apiKey: string;
  tenant: string;
}

const STATUS_CLS: Record<string, string> = {
  VERIFIED: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
  FAILED: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  REJECTED: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  AWAITING_INPUT: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
};
const DEFAULT_CLS = "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300";

export function TaskList({
  creds,
  activeId,
  refreshKey,
  onSelect,
}: {
  creds: Creds;
  activeId: string | null;
  refreshKey: number;
  onSelect: (workrunId: string) => void;
}) {
  const [items, setItems] = useState<RunView[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    listRuns({ apiKey: creds.apiKey, tenant: creds.tenant })
      .then((rs) => {
        if (!alive) return;
        // store.list_runs is insertion-ordered — newest first for the rail
        setItems([...rs].reverse());
        setErr(null);
      })
      .catch((e: unknown) => {
        if (alive) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [creds.apiKey, creds.tenant, refreshKey]);

  if (err) {
    return (
      <p className="rounded-md border border-red-300 bg-red-50 p-2 text-xs text-red-700 dark:bg-red-950/40 dark:text-red-300">
        {err}
      </p>
    );
  }
  if (!items) {
    return <p className="text-xs text-zinc-400">Đang tải lịch sử…</p>;
  }
  if (items.length === 0) {
    return (
      <p className="text-xs text-zinc-400">
        Chưa có yêu cầu nào — gửi yêu cầu đầu tiên bên phải.
      </p>
    );
  }

  return (
    <ul className="space-y-1.5">
      {items.map((r) => (
        <li key={r.workrun_id}>
          <button
            type="button"
            onClick={() => onSelect(r.workrun_id)}
            className={`w-full rounded-md border p-2 text-left transition-colors ${
              r.workrun_id === activeId
                ? "border-blue-500 bg-blue-50 dark:bg-blue-950/30"
                : "border-zinc-200 bg-white hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:hover:bg-zinc-800"
            }`}
          >
            <p className="line-clamp-2 text-xs font-medium leading-4">
              {r.intent || "(không có mô tả)"}
            </p>
            <p className="mt-1 flex items-center gap-1.5 text-[11px]">
              <span
                className={`rounded-full px-1.5 py-px font-semibold ${
                  STATUS_CLS[r.status] ?? DEFAULT_CLS
                }`}
              >
                {r.status}
              </span>
              <span className="truncate text-zinc-400">{r.outcome_type}</span>
            </p>
          </button>
        </li>
      ))}
    </ul>
  );
}
