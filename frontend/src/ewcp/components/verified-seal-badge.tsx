// VERIFIED seal badge — the banner shown once a run's VerificationManifest
// exists. Only a sealed manifest earns this chrome; exploratory output has
// no badge surface (provenance ≠ verification — it must re-enter through
// the governed lane, POST /tasks).

export function VerifiedSealBadge({ manifestHash }: { manifestHash: string }) {
  return (
    <div className="rounded-md border-2 border-emerald-600 bg-emerald-50 px-3 py-2 text-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200">
      <div className="text-sm font-bold tracking-wide">
        KIỂM CHỨNG — đã niêm phong
      </div>
      <div className="mt-0.5 font-mono text-xs break-all">
        manifest_hash: {manifestHash}
      </div>
    </div>
  );
}
