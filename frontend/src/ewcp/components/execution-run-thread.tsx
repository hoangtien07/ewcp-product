"use client";

// ExecutionRunThread — the ExecutionRun detail surface. Observation is
// the Gateway SSE join route (`run.join_url` — GET, read-only); governed
// truth (decisions, manifest, outcome) is proxied owner-scoped through
// /api/ewcp/runs/{id}/*. Decision buttons gate on the extension's
// user-actor binding report.

import { useCallback, useEffect, useRef, useState } from "react";

import {
  downloadDeliverable,
  getExecutionRun,
  getIdentity,
  getWorkrun,
  joinRunStream,
  type ExecutionRun,
  type RunView,
} from "@/ewcp/api";
import { statusLabel } from "@/ewcp/labels";

import { DecisionCard } from "./decision-card";
import { runStatusLabel } from "./execution-run-card";
import { ManifestCard } from "./manifest-card";
import { StudioCard } from "./studio-card";

const LIVE_STATUSES = new Set(["launching", "running", "pending_interrupt"]);

export function ExecutionRunThread({
  executionRunId,
}: {
  executionRunId: string;
}) {
  const [run, setRun] = useState<ExecutionRun | null>(null);
  const [workrun, setWorkrun] = useState<RunView | null>(null);
  const [canDecide, setCanDecide] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const reloadWorkrun = useCallback(() => {
    if (!run?.workrun_id) return;
    getWorkrun(run.execution_run_id)
      .then(setWorkrun)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  }, [run?.execution_run_id, run?.workrun_id]);

  // identity → decision gating
  useEffect(() => {
    getIdentity()
      .then((id) => setCanDecide(id.user_actor_binding))
      .catch(() => setCanDecide(false));
  }, []);

  // initial load + governed truth
  useEffect(() => {
    let alive = true;
    getExecutionRun(executionRunId)
      .then((r) => {
        if (!alive) return;
        setRun(r);
        if (r.workrun_id) {
          getWorkrun(r.execution_run_id)
            .then((w) => alive && setWorkrun(w))
            .catch(
              (e) =>
                alive && setErr(e instanceof Error ? e.message : String(e)),
            );
        }
      })
      .catch(
        (e) => alive && setErr(e instanceof Error ? e.message : String(e)),
      );
    return () => {
      alive = false;
    };
  }, [executionRunId]);

  // SSE join while the run is live — stream end (or abort) refreshes the
  // record + governed view once, no polling loop.
  useEffect(() => {
    if (!run?.join_url || !run.run_id || !LIVE_STATUSES.has(run.status)) return;
    const ctl = new AbortController();
    abortRef.current = ctl;
    setStreaming(true);
    joinRunStream(run.join_url, {
      signal: ctl.signal,
      onEvent: (ev) => {
        if (ev.event === "end" || ev.event === "error") ctl.abort();
      },
    })
      .catch(() => {
        // stream dropped — the record refresh below still applies
      })
      .finally(() => {
        setStreaming(false);
        getExecutionRun(executionRunId, { refresh: true })
          .then(setRun)
          .catch(() => {
            // refresh best-effort; next state change retries
          });
      });
    return () => ctl.abort();
    // join once per run identity — the finally-refresh re-reads status
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.join_url, run?.run_id]);

  if (!run)
    return <p className="text-xs text-zinc-500">{err ?? "Đang tải run…"}</p>;

  const pending = workrun?.pending_questions ?? [];
  const approvalGateOpen =
    pending.length > 0 &&
    workrun?.status !== "awaiting_approval" &&
    workrun?.status !== "candidate_complete";

  return (
    <div className="space-y-4">
      <header className="rounded-lg border border-zinc-300 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold">
            {runStatusLabel(run.status)}
            {streaming && (
              <span className="ml-2 text-[10px] font-normal text-zinc-400">
                · live
              </span>
            )}
          </h2>
          <span className="text-[10px] text-zinc-400">
            {run.task_mode}
            {run.workrun_id && ` · wr:${run.workrun_id.slice(0, 8)}…`}
          </span>
        </div>
        <p className="mt-1 text-sm whitespace-pre-wrap">{run.intent}</p>
        <p className="mt-1 font-mono text-[10px] text-zinc-400">
          {run.execution_run_id} · thread {run.thread_id.slice(0, 8)}…
          {run.run_id ? ` · run ${run.run_id.slice(0, 8)}…` : ""}
        </p>
      </header>

      {err && <p className="text-xs text-red-600">{err}</p>}

      {run.workrun_id && workrun && (
        <section className="space-y-3">
          <p className="text-xs text-zinc-500">
            Kernel: <b>{statusLabel(workrun.status)}</b>
            {workrun.step_label ? ` — ${workrun.step_label}` : ""}
          </p>
          {pending.map((q) => (
            <DecisionCard
              key={q.decision_id}
              q={q}
              executionRunId={run.execution_run_id}
              run={workrun}
              canDecide={canDecide}
              disabledHint={
                q.kind === "approval" && approvalGateOpen
                  ? "Còn câu hỏi chưa trả lời — duyệt sau khi hoàn tất."
                  : undefined
              }
              onDone={setWorkrun}
            />
          ))}
          {workrun.deliverables.length > 0 && (
            <ul className="space-y-1">
              {workrun.deliverables.map((d) => (
                <li key={d.deliverable_id}>
                  <button
                    type="button"
                    onClick={() =>
                      void downloadDeliverable(
                        run.execution_run_id,
                        d.deliverable_id,
                        { name: d.name },
                      )
                    }
                    className="text-xs text-blue-600 underline"
                  >
                    {d.name}
                  </button>
                </li>
              ))}
            </ul>
          )}
          <StudioCard run={workrun} executionRunId={run.execution_run_id} />
          <ManifestCard executionRunId={run.execution_run_id} run={workrun} />
        </section>
      )}
      {run.workrun_id && !workrun && !err && (
        <p className="text-xs text-zinc-500">Đang tải workrun…</p>
      )}
      {workrun && (
        <button
          type="button"
          onClick={reloadWorkrun}
          className="text-xs text-zinc-400 underline"
        >
          Làm mới
        </button>
      )}
    </div>
  );
}
