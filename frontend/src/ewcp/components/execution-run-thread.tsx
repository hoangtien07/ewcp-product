"use client";

// ExecutionRunThread — the ExecutionRun detail surface. Observation is
// the Gateway SSE join route (`run.join_url` — GET, read-only); governed
// truth (decisions, manifest, outcome) is proxied owner-scoped through
// /api/ewcp/runs/{id}/*. Decision buttons gate on the extension's
// user-actor binding report.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  downloadDeliverable,
  getExecutionRun,
  getIdentity,
  getWorkrun,
  joinRunStream,
  resumeRun,
  type ExecutionRun,
  type RunView,
  type RunStreamEvent,
} from "@/ewcp/api";
import { lifecycleStatus, statusLabel, taskModeLabel } from "@/ewcp/labels";

import { DecisionCard } from "./decision-card";
import { LifecycleBadge } from "./lifecycle-badge";
import { ManifestCard } from "./manifest-card";
import { StudioCard } from "./studio-card";

const LIVE_STATUSES = new Set(["launching", "running", "pending_interrupt"]);

// Bounded render of parsed SSE frames — the run detail is blind
// without them (A6 proposal #5): the raw event log is the floor before
// structured agent-activity rendering lands.
const MAX_STREAM_EVENTS = 50;

function summarizeEventData(data: unknown): string {
  if (typeof data === "string") return data;
  try {
    return JSON.stringify(data) ?? "";
  } catch {
    return "[unserializable event payload]";
  }
}

export function ExecutionRunThread({
  executionRunId,
  onWorkrunChange,
}: {
  executionRunId: string;
  /** fired whenever the kernel workrun view is refreshed (initial
   * load, stream-end refresh, and every resolved decision) — the
   * parent bumps its list refetch so badges track kernel truth. */
  onWorkrunChange?: (workrun: RunView) => void;
}) {
  const [run, setRun] = useState<ExecutionRun | null>(null);
  const [workrun, setWorkrun] = useState<RunView | null>(null);
  const [canDecide, setCanDecide] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [streamEvents, setStreamEvents] = useState<RunStreamEvent[]>([]);
  const [resumeText, setResumeText] = useState("");
  const [resuming, setResuming] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const reloadWorkrun = useCallback(() => {
    if (!run?.workrun_id) return;
    getWorkrun(run.execution_run_id)
      .then(setWorkrun)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  }, [run?.execution_run_id, run?.workrun_id]);

  const handleWorkrunDone = useCallback(
    (w: RunView) => {
      setWorkrun(w);
      onWorkrunChange?.(w);
    },
    [onWorkrunChange],
  );

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
        setStreamEvents((prev) => [...prev, ev].slice(-MAX_STREAM_EVENTS));
        if (ev.event === "end" || ev.event === "error") ctl.abort();
      },
    })
      .catch(() => {
        // stream dropped — the record refresh below still applies
      })
      .finally(() => {
        setStreaming(false);
        getExecutionRun(executionRunId, { refresh: true })
          .then((r) => {
            setRun(r);
            // the governed view is kernel-side truth and can transition
            // while the product stream is still open (kernel-first
            // dispatch) — re-read it here or a mount-time "running"
            // would stay stale until remount.
            if (r.workrun_id) {
              getWorkrun(r.execution_run_id)
                .then(setWorkrun)
                .catch(() => {
                  // best-effort like the record refresh
                });
            }
          })
          .catch(() => {
            // refresh best-effort; next state change retries
          });
      });
    return () => ctl.abort();
    // join once per run identity — the finally-refresh re-reads status
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.join_url, run?.run_id]);

  const submitResume = useCallback(async () => {
    const text = resumeText.trim();
    if (!run || !text || resuming) return;
    setResuming(true);
    setErr(null);
    try {
      // The interrupt site owns the payload schema — accept JSON text or
      // pass the plain string through (Command(resume=...) upstream).
      let payload: unknown = text;
      try {
        payload = JSON.parse(text);
      } catch {
        // plain text stays a string
      }
      const updated = await resumeRun(run.execution_run_id, payload);
      setRun(updated);
      setResumeText("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setResuming(false);
    }
  }, [run, resumeText, resuming]);

  if (!run)
    return (
      <p className="text-muted-foreground text-xs">{err ?? "Đang tải run…"}</p>
    );

  const pending = workrun?.pending_questions ?? [];
  const approvalGateOpen =
    pending.length > 0 &&
    workrun?.status !== "awaiting_approval" &&
    workrun?.status !== "candidate_complete";

  // candidate_complete/awaiting_approval is the final human gate: the
  // kernel accepts an approval POST with decision_id null even when the
  // run view carries no pending question (a clean governed run emits
  // none — see docs/vnext/A6_BROWSER_E2E.md). Synthesize the card so the
  // seal path stays reachable from this surface; an emitted approval
  // question still wins when one exists.
  const showApprovalGate =
    (workrun?.status === "candidate_complete" ||
      workrun?.status === "awaiting_approval") &&
    !pending.some((q) => q.kind === "approval");

  return (
    <div className="space-y-4">
      <header className="border-border bg-card rounded-lg border p-4">
        <div className="flex items-center justify-between gap-2">
          {/* the WP-A6 lifecycle claim — one vocabulary over the launcher
              and kernel enums; the raw kernel sub-state stays below as a
              detail annotation */}
          <h2 className="flex items-center text-sm font-semibold">
            <LifecycleBadge status={lifecycleStatus(run, workrun)} />
            {streaming && (
              <span className="text-muted-foreground ml-2 text-[10px] font-normal">
                · trực tiếp
              </span>
            )}
          </h2>
          <span className="text-muted-foreground text-[10px]">
            {taskModeLabel(run.task_mode)}
            {run.workrun_id && ` · wr:${run.workrun_id.slice(0, 8)}…`}
          </span>
        </div>
        <p className="mt-1 text-sm whitespace-pre-wrap">{run.intent}</p>
        <p className="text-muted-foreground mt-1 font-mono text-[10px]">
          {run.execution_run_id} ·{" "}
          <Link
            href={`/workspace/chats/${encodeURIComponent(run.thread_id)}`}
            className="text-primary underline"
            title={run.thread_id}
          >
            thread {run.thread_id.slice(0, 8)}…
          </Link>
          {run.run_id ? ` · run ${run.run_id.slice(0, 8)}…` : ""}
        </p>
      </header>

      {err && <p className="text-destructive text-xs">{err}</p>}

      {run.status === "pending_interrupt" && (
        <section className="space-y-2 rounded-lg border border-amber-300 bg-amber-50/60 p-4 dark:border-amber-800 dark:bg-amber-950/30">
          <h3 className="text-xs font-semibold text-amber-700 dark:text-amber-300">
            Run đang chờ câu trả lời (interrupt)
          </h3>
          <Textarea
            value={resumeText}
            onChange={(e) => setResumeText(e.target.value)}
            rows={2}
            placeholder="Câu trả lời — text thuần hoặc JSON theo contract của interrupt"
          />
          <Button
            type="button"
            size="sm"
            disabled={resuming || !resumeText.trim()}
            onClick={() => void submitResume()}
            className="bg-amber-600 text-white hover:bg-amber-700"
          >
            {resuming ? "Đang gửi…" : "Tiếp tục"}
          </Button>
        </section>
      )}

      {streamEvents.length > 0 && (
        <details className="border-border rounded-lg border p-3">
          <summary className="text-muted-foreground cursor-pointer text-xs font-medium">
            Hoạt động agent ({streamEvents.length} sự kiện)
          </summary>
          <ul className="mt-2 space-y-1">
            {streamEvents.map((ev, i) => (
              <li
                key={i}
                className="text-muted-foreground font-mono text-[10px]"
              >
                <span className="text-foreground font-semibold">
                  {ev.event}
                </span>{" "}
                {summarizeEventData(ev.data).slice(0, 200)}
              </li>
            ))}
          </ul>
        </details>
      )}

      {run.workrun_id && workrun && (
        <section className="space-y-3">
          {/* the kernel's raw sub-state as a detail line — distinct from
              the lifecycle pill (agent_finished covers both
              candidate_complete and awaiting_approval, and the
              distinction is worth seeing) */}
          <p className="text-muted-foreground text-xs">
            Kernel: <b>{statusLabel(workrun.status)}</b>
            {workrun.step_label ? ` — ${workrun.step_label}` : ""}
          </p>
          {pending.map((q) => (
            <DecisionCard
              key={q.decision_id ?? q.kind}
              q={q}
              executionRunId={run.execution_run_id}
              run={workrun}
              canDecide={canDecide}
              disabledHint={
                q.kind === "approval" && approvalGateOpen
                  ? "Còn câu hỏi chưa trả lời — duyệt sau khi hoàn tất."
                  : undefined
              }
              onDone={handleWorkrunDone}
            />
          ))}
          {showApprovalGate && (
            <DecisionCard
              q={{
                decision_id: null,
                kind: "approval",
                prompt:
                  "Agent đã hoàn tất — duyệt để niêm phong kết quả (manifest + seal).",
                options: ["approve", "reject"],
              }}
              executionRunId={run.execution_run_id}
              run={workrun}
              canDecide={canDecide}
              onDone={handleWorkrunDone}
            />
          )}
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
                    className="text-primary text-xs underline"
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
        <p className="text-muted-foreground text-xs">Đang tải workrun…</p>
      )}
      {workrun && (
        <button
          type="button"
          onClick={reloadWorkrun}
          className="text-muted-foreground text-xs underline"
        >
          Làm mới
        </button>
      )}
    </div>
  );
}
