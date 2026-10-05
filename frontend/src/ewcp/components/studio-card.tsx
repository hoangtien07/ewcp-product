"use client";

// StudioCard — per-outcome rich surface fed by GET /workruns/{id}/outcome.
// invoice_recon → exception-first recon table; dossier_check → verdict +
// checklist. Renders nothing until the run has deliverables (export stage
// is what writes the outcome JSON the card reads).

import { useEffect, useState } from "react";

import { getOutcome, type RunView } from "@/ewcp/api";

interface ReconRow {
  status: string;
  source: string;
  sources: string[];
  confidence: number;
  detail: string;
}

interface DossierRow {
  check: string;
  status: string; // pass | warn | fail
  detail: string;
  doc_refs: string[];
}

interface DossierDoc {
  file: string;
  doc_type: string;
}

interface DossierResult {
  verdict: string; // pass | fail
  rows: DossierRow[];
  documents: DossierDoc[];
}

const RECON_CHIP: Record<string, string> = {
  MATCHED: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
  AMOUNT_MISMATCH: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  UNVERIFIED_SOURCE: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
};
const EXC_CHIP = "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300";

const ROW_ICON: Record<string, string> = {
  pass: "text-emerald-600",
  warn: "text-amber-600",
  fail: "text-red-600",
};

function Chip({ label, cls }: { label: string; cls: string }) {
  return (
    <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${cls}`}>
      {label}
    </span>
  );
}

function ReconStudio({ rows }: { rows: ReconRow[] }) {
  const matched = rows.filter((r) => r.status === "MATCHED");
  const exceptions = rows.filter((r) => r.status !== "MATCHED");
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Bảng đối soát
        </span>
        <span className="text-xs text-zinc-500">
          {matched.length}/{rows.length} khớp · {exceptions.length} lệch
        </span>
      </div>
      {exceptions.length === 0 ? (
        <p className="text-sm text-emerald-700 dark:text-emerald-300">
          Mọi dòng đều khớp — không có ngoại lệ.
        </p>
      ) : (
        <ul className="divide-y divide-zinc-200 text-sm dark:divide-zinc-700">
          {exceptions.map((r, i) => (
            <li key={i} className="py-2">
              <div className="flex items-center gap-2">
                <Chip label={r.status} cls={RECON_CHIP[r.status] ?? EXC_CHIP} />
                <span className="font-mono text-xs">{r.source}</span>
                <span className="ml-auto text-[11px] text-zinc-400">
                  {Math.round(r.confidence * 100)}%
                </span>
              </div>
              {r.detail && (
                <p className="mt-0.5 text-xs text-zinc-500">{r.detail}</p>
              )}
              {r.sources.length > 1 && (
                <p className="mt-0.5 font-mono text-[11px] text-zinc-400">
                  {r.sources.join(" ⇄ ")}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DossierStudio({ result }: { result: DossierResult }) {
  const fails = result.rows.filter((r) => r.status === "fail").length;
  const warns = result.rows.filter((r) => r.status === "warn").length;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Checklist hồ sơ
        </span>
        <Chip
          label={
            result.verdict === "pass"
              ? "Hồ sơ đạt"
              : `Hồ sơ thiếu/sai — ${fails} lỗi`
          }
          cls={
            result.verdict === "pass"
              ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
              : "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300"
          }
        />
      </div>
      {warns > 0 && (
        <p className="text-xs text-amber-700 dark:text-amber-300">
          {warns} cảnh báo — cần người xem xét.
        </p>
      )}
      <ul className="divide-y divide-zinc-200 text-sm dark:divide-zinc-700">
        {result.rows.map((r, i) => (
          <li key={i} className="flex gap-2 py-2">
            <span className={`font-bold ${ROW_ICON[r.status] ?? "text-zinc-400"}`}>
              {r.status === "pass" ? "✓" : r.status === "warn" ? "!" : "✗"}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-xs font-semibold">{r.check}</p>
              {r.detail && (
                <p className="mt-0.5 text-xs text-zinc-500">{r.detail}</p>
              )}
              {r.doc_refs.length > 0 && (
                <p className="mt-0.5 font-mono text-[11px] text-zinc-400">
                  {r.doc_refs.join(" · ")}
                </p>
              )}
            </div>
          </li>
        ))}
      </ul>
      {result.documents.length > 0 && (
        <div className="border-t border-zinc-200 pt-2 dark:border-zinc-700">
          <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-zinc-500">
            Chứng từ đã phân loại
          </p>
          <ul className="space-y-0.5 font-mono text-[11px] text-zinc-500">
            {result.documents.map((d, i) => (
              <li key={i} className="flex gap-2">
                <span className="truncate">{d.file}</span>
                <span className="ml-auto shrink-0 rounded bg-zinc-100 px-1.5 dark:bg-zinc-800">
                  {d.doc_type}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function StudioCard({ run, apiKey }: { run: RunView; apiKey: string }) {
  const [result, setResult] = useState<unknown>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setResult(null);
    setFailed(false);
    if (run.deliverables.length === 0) return;
    let alive = true;
    getOutcome(run.workrun_id, { apiKey })
      .then((o) => {
        if (alive) setResult(o.result);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
    // refetch only when the run's exports may have changed — a rerun from
    // the "sửa file rồi chạy lại" path keeps the same workrun_id but writes
    // fresh deliverables, which is why length is in the dep list.
  }, [run.workrun_id, run.deliverables.length, apiKey]);

  if (run.deliverables.length === 0 || failed || !result) return null;

  const body =
    run.outcome_type === "invoice_recon" && Array.isArray(result) ? (
      <ReconStudio rows={result as ReconRow[]} />
    ) : run.outcome_type === "dossier_check" &&
      typeof result === "object" &&
      result !== null ? (
      <DossierStudio result={result as DossierResult} />
    ) : null;

  if (!body) return null;
  return (
    <div className="rounded-lg border border-zinc-300 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
      {body}
    </div>
  );
}
