"use client";

// ExecutionRunList — the owner's ExecutionRunMap rows, newest first.
// Refresh is explicit (the map is durable; SSE covers live updates on
// the detail surface).

import { useCallback, useEffect, useState } from "react";

import { listExecutionRuns, type ExecutionRun } from "@/ewcp/api";

import { ExecutionRunCard } from "./execution-run-card";

export function ExecutionRunList({
  activeId,
  onSelect,
  refreshKey = 0,
}: {
  activeId?: string;
  onSelect: (run: ExecutionRun) => void;
  /** bump to re-fetch (e.g. after launching) */
  refreshKey?: number;
}) {
  const [runs, setRuns] = useState<ExecutionRun[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    listExecutionRuns()
      .then(setRuns)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(load, [load, refreshKey]);

  if (err) return <p className="text-destructive text-xs">{err}</p>;
  if (runs === null)
    return <p className="text-muted-foreground text-xs">Đang tải…</p>;
  if (runs.length === 0)
    return (
      <p className="text-muted-foreground text-xs">Chưa có công việc nào.</p>
    );

  return (
    <ul className="space-y-2">
      {runs.map((r) => (
        <li key={r.execution_run_id}>
          <ExecutionRunCard
            run={r}
            active={r.execution_run_id === activeId}
            onSelect={onSelect}
          />
        </li>
      ))}
    </ul>
  );
}
