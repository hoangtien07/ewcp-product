"use client";

// VerifyView — verify-by-them surface. The verifier downloads the evidence
// export (GET /workruns/{id}/evidence) + the artifacts, optionally TAMPERS
// with a file, then uploads both here → PASS/FAIL per artifact + manifest
// hash recompute. No auth — verification must not require trusting us.

import { useState } from "react";

import { getEvidence, verifyArtifacts, type VerifyResult } from "@/ewcp/api";

export function VerifyView({
  initialRun,
  creds,
}: {
  initialRun?: string;
  creds: { apiKey: string };
}) {
  const [runId, setRunId] = useState(initialRun ?? "");
  const [evidenceFile, setEvidenceFile] = useState<File | null>(null);
  const [artifactFiles, setArtifactFiles] = useState<File[]>([]);
  const [result, setResult] = useState<VerifyResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function downloadEvidence() {
    if (!runId.trim()) return;
    setErr(null);
    try {
      const ev = await getEvidence(runId.trim(), creds);
      const blob = new Blob([JSON.stringify(ev, null, 2)], {
        type: "application/json",
      });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `evidence-${runId.slice(0, 8)}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  async function runVerify() {
    if (!evidenceFile || artifactFiles.length === 0 || busy) return;
    setBusy(true);
    setErr(null);
    setResult(null);
    try {
      setResult(
        await verifyArtifacts({ evidenceJson: evidenceFile, files: artifactFiles }),
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-zinc-300 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
        <h2 className="text-sm font-semibold">
          Xác minh độc lập — verify-by-them
        </h2>
        <p className="mt-1 text-xs text-zinc-500">
          Tải evidence export của run, upload lại cùng các artifact. Hệ thống đo
          lại sha256 từng file + recompute manifest_hash — sửa 1 byte cũng FAIL.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            placeholder="workrun_id"
            className="min-w-56 flex-1 rounded-md border border-zinc-300 bg-transparent px-2 py-1.5 font-mono text-xs dark:border-zinc-600"
          />
          <button
            onClick={downloadEvidence}
            disabled={!runId.trim()}
            className="rounded-md bg-zinc-200 px-3 py-1.5 text-xs font-medium hover:bg-zinc-300 disabled:opacity-50 dark:bg-zinc-700 dark:hover:bg-zinc-600"
          >
            Tải evidence export
          </button>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <label className="block cursor-pointer rounded-md border border-dashed border-zinc-400 px-3 py-2 text-sm">
            <span className="text-zinc-600 dark:text-zinc-300">
              evidence.json
            </span>
            <span className="ml-2 font-mono text-xs text-zinc-400">
              {evidenceFile?.name ?? "chọn file…"}
            </span>
            <input
              type="file"
              accept=".json,application/json"
              className="hidden"
              onChange={(e) =>
                setEvidenceFile(e.target.files?.[0] ?? null)
              }
            />
          </label>
          <label className="block cursor-pointer rounded-md border border-dashed border-zinc-400 px-3 py-2 text-sm">
            <span className="text-zinc-600 dark:text-zinc-300">
              artifacts (xlsx, json…)
            </span>
            <span className="ml-2 font-mono text-xs text-zinc-400">
              {artifactFiles.length > 0
                ? `${artifactFiles.length} file`
                : "chọn nhiều file…"}
            </span>
            <input
              type="file"
              multiple
              className="hidden"
              onChange={(e) =>
                setArtifactFiles(Array.from(e.target.files ?? []))
              }
            />
          </label>
        </div>
        <button
          onClick={runVerify}
          disabled={busy || !evidenceFile || artifactFiles.length === 0}
          className="mt-3 rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          {busy ? "Đang xác minh…" : "Xác minh"}
        </button>
        {err && <p className="mt-2 text-xs text-red-600">{err}</p>}
      </div>

      {result && (
        <div
          className={`rounded-lg border-2 p-4 ${
            result.ok && result.manifest_ok
              ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-950/40"
              : "border-red-600 bg-red-50 dark:bg-red-950/40"
          }`}
        >
          <div
            className={`text-lg font-bold ${
              result.ok && result.manifest_ok
                ? "text-emerald-700 dark:text-emerald-300"
                : "text-red-700 dark:text-red-300"
            }`}
          >
            {result.ok && result.manifest_ok
              ? "PASS — bằng chứng nguyên vẹn"
              : "FAIL — bằng chứng bị thay đổi"}
          </div>
          {!result.manifest_ok && (
            <p className="mt-1 text-xs text-red-700 dark:text-red-300">
              manifest_hash không khớp — evidence export đã bị sửa.
            </p>
          )}
          <ul className="mt-3 space-y-1 text-xs">
            {result.artifacts.map((a) => (
              <li key={a.deliverable_id} className="flex gap-2">
                <span
                  className={a.ok ? "text-emerald-600" : "text-red-600"}
                >
                  {a.ok ? "OK" : a.missing ? "MISSING" : "MISMATCH"}
                </span>
                <span className="font-mono">{a.name}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
