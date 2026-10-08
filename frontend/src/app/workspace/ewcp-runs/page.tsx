"use client";

// ExecutionRun surface — list + intent intake + detail on the SSE
// join route. Thin host for Task 6; Task 7 rehomes it into the
// workspace views.

import { useCallback, useEffect, useState } from "react";

import {
  launchExecutionRun,
  listOutcomes,
  type ExecutionRun,
  type OutcomeSpecView,
} from "@/ewcp/api";
import { CapabilityGallery } from "@/ewcp/components/capability-gallery";
import { ExecutionRunList } from "@/ewcp/components/execution-run-list";
import { ExecutionRunThread } from "@/ewcp/components/execution-run-thread";
import { FALLBACK_SPECS } from "@/ewcp/registry";

export default function EwcpRunsPage() {
  const [intent, setIntent] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [specs, setSpecs] = useState<OutcomeSpecView[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    listOutcomes()
      .then(setSpecs)
      .catch(() => setSpecs(FALLBACK_SPECS));
  }, []);

  const submit = useCallback(async () => {
    const text = intent.trim();
    if (!text || busy) return;
    setBusy(true);
    setErr(null);
    try {
      const { run } = await launchExecutionRun({ intent: text });
      setIntent("");
      setActiveId(run.execution_run_id);
      setRefreshKey((k) => k + 1);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [intent, busy]);

  return (
    <div className="mx-auto grid max-w-5xl gap-6 p-6 md:grid-cols-[280px_1fr]">
      <aside className="space-y-3">
        <h1 className="text-sm font-semibold">ExecutionRuns</h1>
        <ExecutionRunList
          activeId={activeId ?? undefined}
          refreshKey={refreshKey}
          onSelect={(r: ExecutionRun) => setActiveId(r.execution_run_id)}
        />
      </aside>
      <main className="space-y-4">
        <div className="rounded-lg border border-zinc-300 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
          <textarea
            value={intent}
            onChange={(e) => setIntent(e.target.value)}
            rows={2}
            placeholder="Mô tả việc cần làm — vd: đối soát hóa đơn kỳ 09/2025"
            className="w-full rounded-md border border-zinc-300 bg-transparent px-2 py-1.5 text-sm dark:border-zinc-600"
          />
          <div className="mt-2 flex items-center gap-2">
            <button
              onClick={() => void submit()}
              disabled={busy || !intent.trim()}
              className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {busy ? "Đang chạy…" : "Chạy"}
            </button>
            {err && <p className="text-xs text-red-600">{err}</p>}
          </div>
        </div>
        <CapabilityGallery
          specs={specs}
          busy={busy}
          onIntent={setIntent}
        />
        {activeId && <ExecutionRunThread executionRunId={activeId} />}
      </main>
    </div>
  );
}
