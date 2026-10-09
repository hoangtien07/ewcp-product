"use client";

// DecisionCard — renders a pending HumanDecision and posts the answer.
// Covers the ask-back kinds (missing_input handled by file slots) + the
// final approve/reject gate.
//
// Answer contract: options_v2 options post their stable `id` (the kernel
// dispatch key); the legacy `options` label path stays as the fallback
// for kernels before the slug contract.
//
// Gating (plan P1): buttons render only when `canDecide` — the extension
// reports user_actor_binding (kernel honors X-Ewcp-Actor, kernel PR
// #108), so the audit principal binds the product user. Without it the
// card is read-only; the route would 409 anyway.

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { submitDecision, type PendingQuestion, type RunView } from "@/ewcp/api";
import { decisionLabel } from "@/ewcp/labels";

const KIND_LABEL: Record<string, string> = {
  option_choice: "Chọn phương án xử lý",
  anomaly_confirm: "Xác nhận giá trị bất thường",
  confirm_value: "Xác nhận giá trị bất thường",
  approval: "Phê duyệt kết quả",
  missing_input: "Cần bổ sung đầu vào",
  contract_approval: "Duyệt tiêu chí nghiệm thu",
};

export function DecisionCard({
  q,
  executionRunId,
  run,
  canDecide,
  onDone,
  disabledHint,
}: {
  q: PendingQuestion;
  executionRunId: string;
  run: RunView;
  /** user-actor binding live (GET /api/ewcp/identity) — gates buttons. */
  canDecide: boolean;
  onDone: (run: RunView) => void;
  // when set (approval gate while questions are still open — kernel 409s
  // a decision posted early), buttons stay rendered but inert
  disabledHint?: string;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const runRef = useRef(run);

  // an updated run means the situation moved on (e.g. another card's
  // questions got answered) — a rejection from before that no longer
  // describes the current state, so drop it instead of showing stale text
  useEffect(() => {
    runRef.current = run;
    setErr(null);
  }, [run]);

  const options: { id: string; label: string }[] =
    q.options_v2 && q.options_v2.length > 0
      ? q.options_v2
      : (q.options.length > 0
          ? q.options
          : q.kind === "approval"
            ? ["approve", "reject", "request_changes"]
            : ["approve", "reject"]
        ).map((label) => ({ id: label, label }));

  const inert = !canDecide || disabledHint !== undefined;

  async function submit(answer: string) {
    setBusy(answer);
    setErr(null);
    try {
      const next = await submitDecision(executionRunId, {
        answer,
        decisionId: q.decision_id ?? undefined,
      });
      onDone(next);
    } catch (e) {
      // a rejection arriving after the run already advanced is stale —
      // don't resurrect it beside the now-valid action
      if (runRef.current === run) {
        setErr(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="border-border bg-card rounded-lg border p-4 shadow-sm">
      <div className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
        {KIND_LABEL[q.kind] ?? q.kind}
      </div>
      <p className="mt-1 text-sm whitespace-pre-wrap">{q.prompt}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        {options.map((opt) => (
          <Button
            key={opt.id}
            size="sm"
            variant={
              opt.id === "approve" || opt.id === "approve_contract"
                ? "default"
                : opt.id === "reject"
                  ? "destructive"
                  : "secondary"
            }
            disabled={busy !== null || inert}
            onClick={() => void submit(opt.id)}
            className={
              opt.id === "approve" || opt.id === "approve_contract"
                ? "bg-emerald-600 text-white hover:bg-emerald-700"
                : undefined
            }
          >
            {busy === opt.id ? "…" : decisionLabel(opt.label)}
          </Button>
        ))}
      </div>
      {!canDecide && (
        <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
          Chỉ xem — quyết định cần kernel có user-actor binding (X-Ewcp-Actor).
        </p>
      )}
      {canDecide && disabledHint && (
        <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
          {disabledHint}
        </p>
      )}
      {err && <p className="text-destructive mt-2 text-xs">{err}</p>}
    </div>
  );
}
