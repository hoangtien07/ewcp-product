"use client";

// GeneralCard — governed general-lane surface (spec 005 / M-EA1).
// Renders whatever the kernel exposes under run.context.general: the
// TaskState projection (§6: goal, plan, facts, open_questions, blockers)
// plus an optional contract summary (§7). Once deliverables exist it
// also fetches GET /workruns/{id}/outcome and offers the raw outcome
// JSON. RECONCILE: the kernel-side shape is still merging — every field
// is read defensively so the card tracks whatever lands.

import { useEffect, useState } from "react";

import { getOutcome, type RunView } from "@/ewcp/api";

interface PlanStep {
  step?: string;
  status?: string;
}

interface Fact {
  k?: string;
  v?: string;
}

interface ContractSummary {
  version?: number;
  criteria_count?: number;
  proposed_by?: string;
  approved_by?: string | null;
}

interface GeneralContext {
  goal?: string | { statement?: string };
  plan?: PlanStep[];
  facts?: Fact[];
  open_questions?: string[];
  blockers?: string[];
  contract?: ContractSummary;
}

const STEP_ICON: Record<string, { icon: string; cls: string }> = {
  done: { icon: "✓", cls: "text-emerald-600" },
  doing: { icon: "…", cls: "text-blue-600" },
  todo: { icon: "○", cls: "text-zinc-400" },
};

function asGeneralContext(value: unknown): GeneralContext | null {
  return typeof value === "object" && value !== null
    ? (value as GeneralContext)
    : null;
}

function goalText(goal: GeneralContext["goal"]): string | null {
  if (typeof goal === "string") return goal;
  if (typeof goal === "object" && goal !== null) {
    return goal.statement ?? null;
  }
  return null;
}

export function GeneralCard({
  run,
  apiKey,
}: {
  run: RunView;
  apiKey: string;
}) {
  const [outcome, setOutcome] = useState<unknown>(null);
  const [outcomeReady, setOutcomeReady] = useState(false);

  const general = asGeneralContext(run.context?.general);
  const deliverableKey = run.deliverables
    .map((d) => `${d.deliverable_id}:${d.sha256}`)
    .join(",");

  useEffect(() => {
    setOutcome(null);
    setOutcomeReady(false);
    if (run.deliverables.length === 0) return;
    let alive = true;
    getOutcome(run.workrun_id, { apiKey })
      .then((o) => {
        if (alive) {
          setOutcome(o.result);
          setOutcomeReady(true);
        }
      })
      .catch(() => {
        // outcome endpoint 404s until the spec serves its result file —
        // the projection above still describes the lane's state
        if (alive) setOutcomeReady(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run.workrun_id, deliverableKey, apiKey]);

  if (!general && !outcomeReady) return null;

  const goal = goalText(general?.goal);
  const plan = general?.plan?.filter((s) => s?.step) ?? [];
  const facts = general?.facts?.filter((f) => f?.k) ?? [];
  const openQuestions = general?.open_questions?.filter(Boolean) ?? [];
  const blockers = general?.blockers?.filter(Boolean) ?? [];
  const contract = general?.contract;

  return (
    <div className="rounded-lg border border-zinc-300 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold tracking-wide text-zinc-500 uppercase">
          Lane tổng quát
        </span>
        {contract && (
          <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[11px] font-semibold text-blue-700 dark:bg-blue-900/40 dark:text-blue-300">
            Tiêu chí nghiệm thu
            {contract.version ? ` v${contract.version}` : ""}
            {contract.criteria_count
              ? ` · ${contract.criteria_count} tiêu chí`
              : ""}
            {contract.approved_by
              ? " · đã duyệt"
              : contract.proposed_by
                ? ` · đề xuất bởi ${contract.proposed_by}`
                : ""}
          </span>
        )}
      </div>

      {goal && <p className="mt-1.5 text-sm font-medium">{goal}</p>}

      {plan.length > 0 && (
        <ul className="mt-2 space-y-1 text-sm">
          {plan.map((s, i) => {
            const meta = STEP_ICON[s.status ?? ""] ?? STEP_ICON.todo;
            return (
              <li key={i} className="flex items-start gap-2">
                <span className={`font-bold ${meta?.cls}`}>{meta?.icon}</span>
                <span className="min-w-0 flex-1 text-xs">{s.step}</span>
              </li>
            );
          })}
        </ul>
      )}

      {facts.length > 0 && (
        <ul className="mt-2 space-y-0.5 font-mono text-[11px] text-zinc-500">
          {facts.map((f, i) => (
            <li key={i}>
              {f.k} = {f.v ?? "—"}
            </li>
          ))}
        </ul>
      )}

      {openQuestions.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-xs text-amber-700 dark:text-amber-300">
          {openQuestions.map((q, i) => (
            <li key={i}>{q}</li>
          ))}
        </ul>
      )}

      {blockers.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-xs text-red-600 dark:text-red-400">
          {blockers.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      )}

      {outcomeReady && (
        <details className="mt-3 border-t border-zinc-200 pt-2 text-xs dark:border-zinc-700">
          <summary className="cursor-pointer text-zinc-500">
            Kết quả outcome (JSON)
          </summary>
          <pre className="mt-1 max-h-64 overflow-auto rounded bg-zinc-50 p-2 text-[11px] dark:bg-zinc-800">
            {JSON.stringify(outcome, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}
