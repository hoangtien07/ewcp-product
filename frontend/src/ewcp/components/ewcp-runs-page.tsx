"use client";

// ExecutionRun surface — list + intake + detail. Lives under
// components/ so both /workspace/ewcp-runs and the deep-linkable
// /workspace/ewcp-runs/[execution_run_id] route render the same page.

import { useCallback, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  launchExecutionRun,
  listOutcomes,
  type ExecutionRun,
  type OutcomeSpecView,
} from "@/ewcp/api";
import { CapabilityGallery } from "@/ewcp/components/capability-gallery";
import { ExecutionRunList } from "@/ewcp/components/execution-run-list";
import { ExecutionRunThread } from "@/ewcp/components/execution-run-thread";
import { GovernedIntake } from "@/ewcp/components/governed-intake";
import { FALLBACK_SPECS, starterIntentsFor } from "@/ewcp/registry";

export function EwcpRunsPage({
  initialActiveId,
}: {
  initialActiveId?: string;
}) {
  const [intent, setIntent] = useState("");
  const [governedSpec, setGovernedSpec] = useState<OutcomeSpecView | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [specs, setSpecs] = useState<OutcomeSpecView[]>([]);
  const [activeId, setActiveId] = useState<string | null>(
    initialActiveId ?? null,
  );
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    listOutcomes()
      .then(setSpecs)
      .catch(() => setSpecs(FALLBACK_SPECS));
  }, []);

  const select = useCallback((r: ExecutionRun) => {
    setActiveId(r.execution_run_id);
    window.history.replaceState(
      null,
      "",
      `/workspace/ewcp-runs/${encodeURIComponent(r.execution_run_id)}`,
    );
  }, []);

  const pickGoverned = useCallback((spec: OutcomeSpecView) => {
    setGovernedSpec(spec);
    setErr(null);
    const starter = starterIntentsFor(spec)[0];
    if (starter) setIntent(starter);
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
      window.history.replaceState(
        null,
        "",
        `/workspace/ewcp-runs/${encodeURIComponent(run.execution_run_id)}`,
      );
      setRefreshKey((k) => k + 1);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [intent, busy]);

  const submitGoverned = useCallback(
    async (slotFiles: Record<string, File[]>) => {
      if (!governedSpec || busy) return;
      setBusy(true);
      setErr(null);
      try {
        const { run } = await launchExecutionRun({
          intent: intent.trim() || governedSpec.description,
          taskMode: "governed",
          outcomeType: governedSpec.outcome_type,
          slotFiles,
        });
        setIntent("");
        setGovernedSpec(null);
        setActiveId(run.execution_run_id);
        window.history.replaceState(
          null,
          "",
          `/workspace/ewcp-runs/${encodeURIComponent(run.execution_run_id)}`,
        );
        setRefreshKey((k) => k + 1);
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      } finally {
        setBusy(false);
      }
    },
    [governedSpec, intent, busy],
  );

  return (
    <div className="mx-auto grid max-w-5xl gap-6 p-6 md:grid-cols-[280px_1fr]">
      <aside className="space-y-3">
        <h1 className="text-sm font-semibold">Công việc</h1>
        <ExecutionRunList
          activeId={activeId ?? undefined}
          refreshKey={refreshKey}
          onSelect={select}
        />
      </aside>
      <main className="space-y-4">
        <div className="border-border bg-card rounded-lg border p-4">
          <Textarea
            value={intent}
            onChange={(e) => setIntent(e.target.value)}
            rows={2}
            placeholder="Mô tả việc cần làm — vd: đối soát hóa đơn kỳ 09/2025"
          />
          <div className="mt-2 flex items-center gap-2">
            <Button
              onClick={() => void submit()}
              disabled={busy || !intent.trim() || governedSpec !== null}
            >
              {busy && !governedSpec ? "Đang chạy…" : "Chạy"}
            </Button>
            {err && <p className="text-destructive text-xs">{err}</p>}
          </div>
        </div>
        {governedSpec && (
          <GovernedIntake
            spec={governedSpec}
            busy={busy}
            onCancel={() => setGovernedSpec(null)}
            onLaunch={(slotFiles) => void submitGoverned(slotFiles)}
          />
        )}
        <CapabilityGallery
          specs={specs}
          busy={busy}
          onIntent={setIntent}
          onGoverned={pickGoverned}
        />
        {activeId && <ExecutionRunThread executionRunId={activeId} />}
      </main>
    </div>
  );
}
