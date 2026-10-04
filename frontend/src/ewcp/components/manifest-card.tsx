"use client";

// ManifestCard — renders the sealed VerificationManifest once a run verifies:
// manifest_hash + validator checks + who decided. Links to the verify surface.

import { useEffect, useState } from "react";

import { getManifest, type RunView } from "@/ewcp/api";

import { VerifiedSealBadge } from "./unverified-badge";

interface ManifestCheck {
  name?: string;
  result?: string;
  detail?: string;
}

export function ManifestCard({
  run,
  creds,
}: {
  run: RunView;
  creds: { apiKey: string };
}) {
  const [manifest, setManifest] = useState<Record<string, unknown> | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (run.status !== "VERIFIED") return;
    getManifest(run.workrun_id, creds)
      .then(setManifest)
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  }, [run.workrun_id, run.status, creds.apiKey]); // eslint-disable-line react-hooks/exhaustive-deps

  if (run.status !== "VERIFIED") return null;

  const checks = (manifest?.checks as ManifestCheck[] | undefined) ?? [];
  const hash =
    typeof manifest?.manifest_hash === "string"
      ? manifest.manifest_hash
      : "…";

  return (
    <div className="space-y-3">
      <VerifiedSealBadge manifestHash={hash} />
      {err && <p className="text-xs text-red-600">{err}</p>}
      {checks.length > 0 && (
        <div className="rounded-lg border border-zinc-300 bg-white p-3 text-xs dark:border-zinc-700 dark:bg-zinc-900">
          <div className="mb-2 font-semibold">Validator checks</div>
          <ul className="space-y-1">
            {checks.map((c, i) => (
              <li key={i} className="flex gap-2">
                <span
                  className={
                    c.result === "PASS" ? "text-emerald-600" : "text-red-600"
                  }
                >
                  {c.result ?? "?"}
                </span>
                <span className="flex-1">
                  <span className="font-mono">{c.name}</span>
                  {c.detail && (
                    <span className="text-zinc-500"> — {c.detail}</span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {run.decision && (
        <p className="text-xs text-zinc-500">
          Quyết định: <b>{run.decision.answer}</b> bởi {run.decision.decided_by}
        </p>
      )}
      <a
        href={`/ewcp/verify?run=${run.workrun_id}`}
        className="inline-block text-xs font-medium text-blue-600 underline"
      >
        Mở trang verify — khách tự kiểm chứng →
      </a>
    </div>
  );
}
