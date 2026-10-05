"use client";

// TaskList — the governed workbench's history rail. Lists the tenant's runs
// (GET /workruns?tenant_id=) so an earlier ask-back, approval, or sealed run
// is reachable after reload/navigation — the workspace is not single-shot.

import { useEffect, useState, type ReactNode } from "react";

import { listRuns, type RunView } from "@/ewcp/api";
import { outcomeLabel, statusLabel } from "@/ewcp/labels";

interface Creds {
  apiKey: string;
  tenant: string;
}

// kernel TaskStatus emits lowercase enum values
const STATUS_CLS: Record<string, string> = {
  verified:
    "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
  failed: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  rejected: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  cancelled: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  awaiting_input:
    "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  awaiting_approval:
    "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
};
const DEFAULT_CLS =
  "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300";

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded-full border px-2 py-0.5 text-[11px] font-medium transition-colors ${
        active
          ? "border-blue-500 bg-blue-50 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300"
          : "border-zinc-300 text-zinc-500 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-800"
      }`}
    >
      {children}
    </button>
  );
}

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
  // outcome_type filter — only offered when the history actually spans
  // more than one pack (gap-catalog Catalog UX #5)
  const [filter, setFilter] = useState<string | null>(null);

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

  // registry order follows insertion order of the distinct types seen
  const types = [...new Set(items.map((r) => r.outcome_type))];
  // a stale filter (its type vanished from the rail) falls back to all
  const active = filter && types.includes(filter) ? filter : null;
  const visible = active
    ? items.filter((r) => r.outcome_type === active)
    : items;

  return (
    <div className="space-y-2">
      {types.length > 1 && (
        <div className="flex flex-wrap gap-1">
          <FilterChip active={!active} onClick={() => setFilter(null)}>
            Tất cả
          </FilterChip>
          {types.map((t) => (
            <FilterChip
              key={t}
              active={active === t}
              onClick={() => setFilter(active === t ? null : t)}
            >
              {outcomeLabel(t)}
            </FilterChip>
          ))}
        </div>
      )}
      <ul className="space-y-1.5">
        {visible.map((r) => (
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
              <p className="line-clamp-2 text-xs leading-4 font-medium">
                {r.intent || "(không có mô tả)"}
              </p>
              <p className="mt-1 flex items-center gap-1.5 text-[11px]">
                <span
                  className={`rounded-full px-1.5 py-px font-semibold ${
                    STATUS_CLS[r.status] ?? DEFAULT_CLS
                  }`}
                >
                  {statusLabel(r.status)}
                </span>
                <span className="truncate text-zinc-400">
                  {outcomeLabel(r.outcome_type)}
                </span>
              </p>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
