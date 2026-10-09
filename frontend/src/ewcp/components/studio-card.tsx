"use client";

// StudioCard — per-outcome rich surface fed by GET /workruns/{id}/outcome.
// invoice_recon → exception-first recon table; dossier_check → verdict +
// checklist; three_way_match → PO-line exception rows + doc-level flags.
// Renders nothing until the run has deliverables (export stage
// is what writes the outcome JSON the card reads).

import { useEffect, useState } from "react";

import { getOutcome, type RunView } from "@/ewcp/api";
import {
  reconStatusLabel,
  threeWayFlagLabel,
  threeWayStatusLabel,
} from "@/ewcp/labels";

interface ReconRow {
  status: string;
  source: string;
  sources: string[];
  confidence?: number;
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

// three_way_match/match/match.py MatchRow — quantities arrive as
// preformatted strings or null (Decimal serialized via str()).
interface ThreeWayRow {
  status: string; // kernel MatchStatus value
  severity: string; // info | warn | fail
  source: string; // po:{po_no}:L{n} for PO-line rows; doc rows otherwise
  sources: string[];
  flags: string[];
  po_no: string;
  label: string; // sku/description
  ordered_qty: string | null;
  received_qty: string | null;
  invoiced_qty: string | null;
  ordered_price: string | null;
  invoiced_amount: string | null;
  currency: string;
  detail: string;
}

interface ThreeWayResult {
  verdict: string; // pass | warn | fail
  params?: { buyer_mst?: string; ky?: string };
  rows: ThreeWayRow[];
}

// a PO-line row is identified by its source shape — same classifier the
// kernel's own line_conservation validator uses (spec.py)
const PO_LINE_RE = /^po:.+:L\d+$/;

const RECON_CHIP: Record<string, string> = {
  MATCHED:
    "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
  AMOUNT_MISMATCH:
    "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  UNVERIFIED_SOURCE:
    "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
};
const EXC_CHIP =
  "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300";

const ROW_ICON: Record<string, string> = {
  pass: "text-emerald-600",
  warn: "text-amber-600",
  fail: "text-red-600",
};

// three_way severity → chip color (fail/warn rows are exceptions)
const TW_CHIP: Record<string, string> = {
  fail: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  warn: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  info: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
};

const TW_VERDICT: Record<string, { label: string; cls: string }> = {
  pass: {
    label: "Đạt",
    cls: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
  },
  warn: {
    label: "Có cảnh báo",
    cls: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  },
  fail: {
    label: "Có lỗi — cần xử lý",
    cls: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
  },
};

function Chip({ label, cls }: { label: string; cls: string }) {
  return (
    <span
      className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${cls}`}
    >
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
        <span className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
          Bảng đối soát
        </span>
        <span className="text-muted-foreground text-xs">
          {matched.length}/{rows.length} khớp · {exceptions.length} lệch
        </span>
      </div>
      {exceptions.length === 0 ? (
        <p className="text-sm text-emerald-700 dark:text-emerald-300">
          Mọi dòng đều khớp — không có ngoại lệ.
        </p>
      ) : (
        <ul className="divide-border divide-y text-sm">
          {exceptions.map((r, i) => (
            <li key={i} className="py-2">
              <div className="flex items-center gap-2">
                <Chip
                  label={reconStatusLabel(r.status)}
                  cls={RECON_CHIP[r.status] ?? EXC_CHIP}
                />
                <span className="font-mono text-xs">{r.source}</span>
                {typeof r.confidence === "number" && (
                  <span className="text-muted-foreground ml-auto text-[11px]">
                    {Math.round(r.confidence * 100)}%
                  </span>
                )}
              </div>
              {r.detail && (
                <p className="text-muted-foreground mt-0.5 text-xs">
                  {r.detail}
                </p>
              )}
              {r.sources.length > 1 && (
                <p className="text-muted-foreground mt-0.5 font-mono text-[11px]">
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
        <span className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
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
      <ul className="divide-border divide-y text-sm">
        {result.rows.map((r, i) => (
          <li key={i} className="flex gap-2 py-2">
            <span
              className={`font-bold ${ROW_ICON[r.status] ?? "text-muted-foreground"}`}
            >
              {r.status === "pass" ? "✓" : r.status === "warn" ? "!" : "✗"}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-xs font-semibold">{r.check}</p>
              {r.detail && (
                <p className="text-muted-foreground mt-0.5 text-xs">
                  {r.detail}
                </p>
              )}
              {r.doc_refs.length > 0 && (
                <p className="text-muted-foreground mt-0.5 font-mono text-[11px]">
                  {r.doc_refs.join(" · ")}
                </p>
              )}
            </div>
          </li>
        ))}
      </ul>
      {result.documents.length > 0 && (
        <div className="border-border border-t pt-2">
          <p className="text-muted-foreground mb-1 text-xs font-semibold tracking-wide uppercase">
            Chứng từ đã phân loại
          </p>
          <ul className="text-muted-foreground space-y-0.5 font-mono text-[11px]">
            {result.documents.map((d, i) => (
              <li key={i} className="flex gap-2">
                <span className="truncate">{d.file}</span>
                <span className="bg-muted ml-auto shrink-0 rounded px-1.5">
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

function qty(v: string | null): string {
  return v ?? "—";
}

function ThreeWayStudio({ result }: { result: ThreeWayResult }) {
  const rows = result.rows;
  const matched = rows.filter((r) => r.status === "MATCHED");
  const exceptions = rows.filter((r) => r.status !== "MATCHED");
  const lineRows = exceptions.filter((r) => PO_LINE_RE.test(r.source));
  const docRows = exceptions.filter((r) => !PO_LINE_RE.test(r.source));
  const verdict = TW_VERDICT[result.verdict] ?? {
    label: result.verdict,
    cls: EXC_CHIP,
  };
  // bound run context worth surfacing: reconciliation period + the buyer
  // MST the match was checked against (both optional on the wire)
  const context = [
    result.params?.ky ? `Kỳ ${result.params.ky}` : "",
    result.params?.buyer_mst ? `MST ${result.params.buyer_mst}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-muted-foreground text-xs font-semibold tracking-wide uppercase">
          Đối chiếu 3 chiều
        </span>
        <Chip label={verdict.label} cls={verdict.cls} />
      </div>
      <div className="text-muted-foreground flex items-center justify-between text-xs">
        <span>
          {matched.length}/{rows.length} dòng khớp · {exceptions.length} ngoại
          lệ
        </span>
        {context && <span className="font-mono">{context}</span>}
      </div>
      {exceptions.length === 0 ? (
        <p className="text-sm text-emerald-700 dark:text-emerald-300">
          Mọi dòng khớp đủ 3 chiều — không có ngoại lệ.
        </p>
      ) : (
        <>
          {lineRows.length > 0 && (
            <ul className="divide-border divide-y text-sm">
              {lineRows.map((r, i) => (
                <li key={i} className="py-2">
                  <div className="flex items-center gap-2">
                    <Chip
                      label={threeWayStatusLabel(r.status)}
                      cls={TW_CHIP[r.severity] ?? EXC_CHIP}
                    />
                    <span className="font-mono text-xs font-semibold">
                      {r.po_no || r.source}
                    </span>
                    {r.label && (
                      <span className="text-muted-foreground truncate text-xs">
                        {r.label}
                      </span>
                    )}
                  </div>
                  <p className="text-muted-foreground mt-0.5 text-xs">
                    đặt {qty(r.ordered_qty)} · nhận {qty(r.received_qty)} · HĐ{" "}
                    {qty(r.invoiced_qty)}
                    {r.invoiced_amount &&
                      ` — ${r.invoiced_amount}` +
                        (r.currency ? ` ${r.currency}` : "")}
                  </p>
                  {r.flags.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {r.flags.map((f) => (
                        <span
                          key={f}
                          className="bg-muted text-muted-foreground rounded px-1.5 text-[11px]"
                        >
                          {threeWayFlagLabel(f)}
                        </span>
                      ))}
                    </div>
                  )}
                  {r.detail && (
                    <p className="text-muted-foreground mt-0.5 text-xs">
                      {r.detail}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
          {docRows.length > 0 && (
            <div
              className={
                lineRows.length > 0 ? "border-border border-t pt-2" : ""
              }
            >
              <p className="text-muted-foreground mb-1 text-xs font-semibold tracking-wide uppercase">
                Chứng từ
              </p>
              <ul className="divide-border divide-y text-sm">
                {docRows.map((r, i) => (
                  <li key={i} className="py-2">
                    <div className="flex items-center gap-2">
                      <Chip
                        label={threeWayStatusLabel(r.status)}
                        cls={TW_CHIP[r.severity] ?? EXC_CHIP}
                      />
                      <span className="font-mono text-xs">{r.source}</span>
                    </div>
                    {r.detail && (
                      <p className="text-muted-foreground mt-0.5 text-xs">
                        {r.detail}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export function StudioCard({
  run,
  executionRunId,
}: {
  run: RunView;
  executionRunId: string;
}) {
  const [result, setResult] = useState<unknown>(null);
  const [failed, setFailed] = useState(false);

  // identity of the export set — a rerun ("sửa file rồi chạy lại") replaces
  // deliverables possibly at the same count, so length alone misses it
  const deliverableKey = run.deliverables
    .map((d) => `${d.deliverable_id}:${d.sha256}`)
    .join(",");

  useEffect(() => {
    setResult(null);
    setFailed(false);
    if (run.deliverables.length === 0) return;
    let alive = true;
    getOutcome(executionRunId)
      .then((o) => {
        if (alive) setResult(o.result);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [executionRunId, deliverableKey]); // eslint-disable-line react-hooks/exhaustive-deps

  if (run.deliverables.length === 0 || failed || !result) return null;

  const body =
    run.outcome_type === "invoice_recon" && Array.isArray(result) ? (
      <ReconStudio rows={result as ReconRow[]} />
    ) : run.outcome_type === "dossier_check" &&
      typeof result === "object" &&
      result !== null ? (
      <DossierStudio result={result as DossierResult} />
    ) : run.outcome_type === "three_way_match" &&
      typeof result === "object" &&
      result !== null &&
      Array.isArray((result as ThreeWayResult).rows) ? (
      <ThreeWayStudio result={result as ThreeWayResult} />
    ) : null;

  if (!body) return null;
  return (
    <div className="border-border bg-card rounded-lg border p-4">{body}</div>
  );
}
