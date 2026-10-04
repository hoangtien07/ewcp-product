"use client";

// DecisionCard — renders a pending HumanDecision and posts the answer.
// Covers the 3 ask-back kinds from spec 002 (missing_input handled by
// FileSlotCard, not here) + final approve/reject gate.

import { useState } from "react";

import { decide, type PendingQuestion, type RunView } from "@/ewcp/api";

const KIND_LABEL: Record<string, string> = {
  option_choice: "Chọn phương án xử lý",
  confirm_value: "Xác nhận giá trị bất thường",
  approval: "Phê duyệt kết quả",
  missing_input: "Cần bổ sung đầu vào",
};

export function DecisionCard({
  q,
  run,
  creds,
  onDone,
}: {
  q: PendingQuestion;
  run: RunView;
  creds: { apiKey: string; tenant: string; decidedBy: string };
  onDone: (run: RunView) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const options =
    q.options.length > 0
      ? q.options
      : q.kind === "approval"
        ? ["approve", "reject", "request_changes"]
        : ["approve", "reject"];

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
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="rounded-lg border border-zinc-300 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900">
      <div className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
        {KIND_LABEL[q.kind] ?? q.kind}
      </div>
      <p className="mt-1 text-sm whitespace-pre-wrap">{q.prompt}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        {options.map((opt) => (
          <button
            key={opt}
            disabled={busy !== null}
            onClick={() => submit(opt)}
            className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50 ${
              opt === "approve"
                ? "bg-emerald-600 text-white hover:bg-emerald-700"
                : opt === "reject"
                  ? "bg-red-600 text-white hover:bg-red-700"
                  : "bg-zinc-200 text-zinc-800 hover:bg-zinc-300 dark:bg-zinc-700 dark:text-zinc-100 dark:hover:bg-zinc-600"
            }`}
          >
            {busy === opt ? "…" : opt}
          </button>
        ))}
      </div>
      {err && <p className="mt-2 text-xs text-red-600">{err}</p>}
    </div>
  );
}
