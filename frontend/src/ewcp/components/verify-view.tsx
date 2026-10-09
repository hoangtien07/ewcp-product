"use client";

// VerifyView — verify-by-them surface. Two lanes:
// 1. Permalink (/verify/[hash]): a third party opens a shared link →
//    GET /api/ewcp/verify/{hash} → seal state + manifest contents.
//    No upload — proves the sealed manifest EXISTS.
// 2. Byte-integrity upload: the verifier downloads the evidence export
//    (owner-scoped: GET /api/ewcp/runs/{id}/evidence) + the artifacts,
//    optionally TAMPERS with a file, then uploads both here → PASS/FAIL
//    per artifact + manifest hash recompute.

import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  getEvidence,
  verifyArtifacts,
  verifyPermalink,
  type VerifyPermalinkResult,
  type VerifyResult,
} from "@/ewcp/api";

export function VerifyView({ initialHash }: { initialHash?: string }) {
  const [runId, setRunId] = useState("");
  const [manifestHash, setManifestHash] = useState(initialHash ?? "");
  const [permalink, setPermalink] = useState<VerifyPermalinkResult | null>(
    null,
  );
  const [permalinkErr, setPermalinkErr] = useState<string | null>(null);
  const [permalinkBusy, setPermalinkBusy] = useState(false);
  const permalinkSeq = useRef(0);
  const [evidenceFile, setEvidenceFile] = useState<File | null>(null);
  const [artifactFiles, setArtifactFiles] = useState<File[]>([]);
  const [result, setResult] = useState<VerifyResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function lookupManifest(hash: string) {
    const trimmed = hash.trim();
    if (!trimmed) return;
    const seq = ++permalinkSeq.current;
    setPermalinkBusy(true);
    setPermalinkErr(null);
    setPermalink(null);
    try {
      const res = await verifyPermalink(trimmed);
      if (seq !== permalinkSeq.current) return; // stale lookup
      setPermalink(res);
    } catch (e) {
      if (seq !== permalinkSeq.current) return;
      setPermalinkErr(e instanceof Error ? e.message : String(e));
    } finally {
      if (seq === permalinkSeq.current) setPermalinkBusy(false);
    }
  }

  // share-link landing: /verify/<hash> auto-looks-up
  useEffect(() => {
    if (initialHash) void lookupManifest(initialHash);
    // mount-only: the URL param is the trigger, not reactive state
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function downloadEvidence() {
    const id = runId.trim();
    if (!id) return;
    setErr(null);
    try {
      // owner-scoped route — the verifier must be the run's owner
      const ev = await getEvidence(id);
      const blob = new Blob([JSON.stringify(ev, null, 2)], {
        type: "application/json",
      });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `evidence-${id.slice(0, 8)}.json`;
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
        await verifyArtifacts({
          evidenceJson: evidenceFile,
          files: artifactFiles,
        }),
      );
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="border-border bg-card rounded-lg border p-4">
        <h2 className="text-sm font-semibold">
          Tra cứu bằng chứng niêm phong — link công khai
        </h2>
        <p className="text-muted-foreground mt-1 text-xs">
          Ai có link đều tra cứu được. Link chứng minh manifest đã niêm phong
          tồn tại và seal còn nguyên; kiểm chứng từng byte của file cần evidence
          export + artifacts (form bên dưới).
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Input
            value={manifestHash}
            onChange={(e) => setManifestHash(e.target.value)}
            placeholder="manifest_hash (64 ký tự hex)"
            className="min-w-56 flex-1 font-mono text-xs"
          />
          <Button
            size="sm"
            onClick={() => lookupManifest(manifestHash)}
            disabled={permalinkBusy || !manifestHash.trim()}
          >
            {permalinkBusy ? "Đang tra cứu…" : "Tra cứu"}
          </Button>
        </div>
        {permalinkErr && (
          <p className="text-destructive mt-2 text-xs">{permalinkErr}</p>
        )}
      </div>

      {permalink && (
        <div
          className={`rounded-lg border-2 p-4 ${
            permalink.seal_ok === true
              ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-950/40"
              : permalink.seal_ok === false
                ? "border-red-600 bg-red-50 dark:bg-red-950/40"
                : "border-amber-500 bg-amber-50 dark:bg-amber-950/40"
          }`}
        >
          <div
            className={`text-lg font-bold ${
              permalink.seal_ok === true
                ? "text-emerald-700 dark:text-emerald-300"
                : permalink.seal_ok === false
                  ? "text-red-700 dark:text-red-300"
                  : "text-amber-700 dark:text-amber-300"
            }`}
          >
            {permalink.seal_ok === true
              ? "PASS — niêm phong hợp lệ"
              : permalink.seal_ok === false
                ? "FAIL — seal không khớp"
                : "MANIFEST TỒN TẠI — seal không chứng minh được"}
          </div>
          {permalink.seal_ok === false && (
            <p className="mt-1 text-xs text-red-700 dark:text-red-300">
              Seal HMAC không khớp — manifest có thể đã bị sửa sau niêm phong.
            </p>
          )}
          {permalink.seal_ok === null && (
            <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
              Kernel không cấu hình seal key — chứng minh được manifest tồn tại
              nhưng không chứng minh được tính xác thực.
            </p>
          )}
          <dl className="mt-3 space-y-1 text-xs">
            <div className="flex gap-2">
              <dt className="text-muted-foreground">manifest_hash</dt>
              <dd className="font-mono break-all">
                {permalink.manifest.manifest_hash ?? "…"}
              </dd>
            </div>
            <div className="flex gap-2">
              <dt className="text-muted-foreground">workrun_id</dt>
              <dd className="font-mono">{permalink.workrun_id}</dd>
            </div>
          </dl>
          {(permalink.manifest.checks?.length ?? 0) > 0 && (
            <ul className="mt-3 space-y-1 text-xs">
              {(permalink.manifest.checks ?? []).map((c, i) => (
                <li key={i} className="flex gap-2">
                  <span
                    className={
                      c.result === "PASS"
                        ? "text-emerald-600"
                        : "text-destructive"
                    }
                  >
                    {c.result ?? "?"}
                  </span>
                  <span className="flex-1">
                    <span className="font-mono">{c.name}</span>
                    {c.detail && (
                      <span className="text-muted-foreground">
                        {" "}
                        — {c.detail}
                      </span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {(permalink.manifest.deliverables?.length ?? 0) > 0 && (
            <div className="mt-3">
              <div className="text-muted-foreground text-xs font-semibold">
                Artifacts đã niêm phong
              </div>
              <ul className="mt-1 space-y-1 text-xs">
                {(permalink.manifest.deliverables ?? []).map((d, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="font-mono">
                      {d.name ?? d.deliverable_id}
                    </span>
                    {d.sha256 && (
                      <span className="text-muted-foreground font-mono break-all">
                        sha256:{d.sha256.slice(0, 16)}…
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      <div className="border-border bg-card rounded-lg border p-4">
        <h2 className="text-sm font-semibold">
          Xác minh độc lập — verify-by-them
        </h2>
        <p className="text-muted-foreground mt-1 text-xs">
          Upload evidence export của run cùng các artifact. Hệ thống đo lại
          sha256 từng file + recompute manifest_hash — sửa 1 byte cũng FAIL.
          Evidence export lấy từ trang run của chủ sở hữu (owner-scoped).
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Input
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            placeholder="execution_run_id (chủ run)"
            className="min-w-56 flex-1 font-mono text-xs"
          />
          <Button
            size="sm"
            variant="secondary"
            onClick={downloadEvidence}
            disabled={!runId.trim()}
          >
            Tải evidence export
          </Button>
        </div>
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          <label className="border-border block cursor-pointer rounded-md border border-dashed px-3 py-2 text-sm">
            <span>evidence.json</span>
            <span className="text-muted-foreground ml-2 font-mono text-xs">
              {evidenceFile?.name ?? "chọn file…"}
            </span>
            <input
              type="file"
              accept=".json,application/json"
              className="hidden"
              onChange={(e) => setEvidenceFile(e.target.files?.[0] ?? null)}
            />
          </label>
          <label className="border-border block cursor-pointer rounded-md border border-dashed px-3 py-2 text-sm">
            <span>artifacts (xlsx, json…)</span>
            <span className="text-muted-foreground ml-2 font-mono text-xs">
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
        <Button
          className="mt-3"
          onClick={runVerify}
          disabled={busy || !evidenceFile || artifactFiles.length === 0}
        >
          {busy ? "Đang xác minh…" : "Xác minh"}
        </Button>
        {err && <p className="text-destructive mt-2 text-xs">{err}</p>}
      </div>

      {result && (
        <div
          className={`rounded-lg border-2 p-4 ${
            result.verdict === "PASS" && result.manifest_ok
              ? "border-emerald-600 bg-emerald-50 dark:bg-emerald-950/40"
              : "border-red-600 bg-red-50 dark:bg-red-950/40"
          }`}
        >
          <div
            className={`text-lg font-bold ${
              result.verdict === "PASS" && result.manifest_ok
                ? "text-emerald-700 dark:text-emerald-300"
                : "text-red-700 dark:text-red-300"
            }`}
          >
            {result.verdict === "PASS" && result.manifest_ok
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
                  className={a.ok ? "text-emerald-600" : "text-destructive"}
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
