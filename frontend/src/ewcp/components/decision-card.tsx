"use client";

// DecisionCard — renders a pending HumanDecision and posts the answer.
// Covers the 3 ask-back kinds from spec 002 (missing_input handled by
// FileSlotCard, not here) + final approve/reject gate.
//
// Answer contract: options_v2 options post their stable `id` (the kernel
// dispatch key); the legacy `options` label path stays as the fallback
// for kernels before the slug contract.

import { useEffect, useRef, useState } from "react";

import { decide, type PendingQuestion, type RunView } from "@/ewcp/api";
import { decisionLabel } from "@/ewcp/labels";

const KIND_LABEL: Record<string, string> = {
  option_choice: "Chọn phương án xử lý",
  anomaly_confirm: "Xác nhận giá trị bất thường",
  confirm_value: "Xác nhận giá trị bất thường",
  approval: "Phê duyệt kết quả",
  missing_input: "Cần bổ sung đầu vào",
  // spec 005 §7.3 — general lane proposes the AcceptanceContract, the
  // operator approves/revises it before execution continues
  contract_approval: "Duyệt tiêu chí nghiệm thu",
};

export function DecisionCard({
  q,
  run,
  creds,
  onDone,
  disabledHint,
}: {
  q: PendingQuestion;
  run: RunView;
  creds: { apiKey: string; tenant: string; decidedBy: string };
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

  // options_v2 = slug contract (post .id); bare `options` labels are the
  // legacy wire form — treated as id == label so old kernels still work
  const options: { id: string; label: string }[] =
    q.options_v2 && q.options_v2.length > 0
      ? q.options_v2
      : (q.options.length > 0
          ? q.options
          : q.kind === "approval"
            ? ["approve", "reject", "request_changes"]
            : ["approve", "reject"]
        ).map((label) => ({ id: label, label }));

  async function submit(answer: string) {
    setBusy(answer);
    setErr(null);
    try {
      const next = await decide(run.workrun_id, {
        apiKey: creds.apiKey,
        tenant: creds.tenant,
        decidedBy: creds.decidedBy,
        answer,
        decisionId: q.decision_id,
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
    <div className="rounded-lg border border-zinc-300 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
      <div className="text-xs font-semibold tracking-wide text-zinc-500 uppercase">
        {KIND_LABEL[q.kind] ?? q.kind}
      </div>
      <p className="mt-1 text-sm whitespace-pre-wrap">{q.prompt}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        {options.map((opt) => (
          <button
            key={opt.id}
            disabled={busy !== null || disabledHint !== undefined}
            onClick={() => void submit(opt.id)}
            className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50 ${
              opt.id === "approve" || opt.id === "approve_contract"
                ? "bg-emerald-600 text-white hover:bg-emerald-700"
                : opt.id === "reject"
                  ? "bg-red-600 text-white hover:bg-red-700"
                  : "bg-zinc-200 text-zinc-800 hover:bg-zinc-300 dark:bg-zinc-700 dark:text-zinc-100 dark:hover:bg-zinc-600"
            }`}
          >
            {busy === opt.id ? "…" : decisionLabel(opt.label)}
          </button>
        ))}
      </div>
      {disabledHint && (
        <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
          {disabledHint}
        </p>
      )}
      {err && <p className="mt-2 text-xs text-red-600">{err}</p>}
    </div>
  );
}
