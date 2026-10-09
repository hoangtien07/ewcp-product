"use client";

// ManifestCard — renders the sealed VerificationManifest once a run
// verifies: manifest_hash + validator checks + who decided. Links to the
// public verify page.

import { useEffect, useState } from "react";

import { getManifest, type RunView } from "@/ewcp/api";
import { decisionLabel } from "@/ewcp/labels";

import { ShareVerifyLink } from "./share-verify-link";
import { VerifiedSealBadge } from "./verified-seal-badge";

interface ManifestCheck {
  name?: string;
  result?: string;
  detail?: string;
}

export function ManifestCard({
  executionRunId,
  run,
}: {
  executionRunId: string;
  run: RunView;
}) {
  const [manifest, setManifest] = useState<Record<string, unknown> | null>(
    null,
  );
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    // never show a previous run's manifest under this run's seal
    setManifest(null);
    setErr(null);
    if (run.status !== "verified") return;
    let alive = true;
    getManifest(executionRunId)
      .then((m) => {
        if (alive) setManifest(m);
      })
      .catch((e) => {
        if (alive) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [executionRunId, run.status]);

  if (run.status !== "verified") return null;

  // never show the seal before the manifest actually loaded — a verified
  // badge with a placeholder hash presents unavailable evidence as sealed
  if (err) {
    return <p className="text-destructive text-xs">{err}</p>;
  }
  if (!manifest) {
    return <p className="text-muted-foreground text-xs">Đang tải manifest…</p>;
  }

  const checks = (manifest.checks as ManifestCheck[] | undefined) ?? [];
  const hash =
    typeof manifest.manifest_hash === "string" ? manifest.manifest_hash : "…";
  const sealed =
    typeof manifest.manifest_hash === "string" &&
    manifest.manifest_hash.length > 0;

  return (
    <div className="space-y-3">
      <VerifiedSealBadge manifestHash={hash} />
      {checks.length > 0 && (
        <div className="border-border bg-card rounded-lg border p-3 text-xs">
          <div className="mb-2 font-semibold">Validator checks</div>
          <ul className="space-y-1">
            {checks.map((c, i) => (
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
                    <span className="text-muted-foreground"> — {c.detail}</span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {run.decision && (
        <p className="text-muted-foreground text-xs">
          Quyết định: <b>{decisionLabel(run.decision.answer)}</b> bởi{" "}
          {run.decision.decided_by}
        </p>
      )}
      {/* share link + verify page only once the real sealed hash is
          known — never a placeholder (same rule as the badge above) */}
      {sealed && (
        <>
          <a
            href={`/verify/${encodeURIComponent(hash)}`}
            className="text-primary inline-block text-xs font-medium underline"
          >
            Mở trang verify — khách tự kiểm chứng →
          </a>
          <ShareVerifyLink manifestHash={hash} />
        </>
      )}
    </div>
  );
}
