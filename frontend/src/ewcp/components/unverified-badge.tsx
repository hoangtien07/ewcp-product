// UNVERIFIED badge — dominates all artifact chrome on exploratory (non-governed)
// artifacts. Binding condition Q24: provenance ≠ verification (Perplexity trap —
// a cited source is not a verified artifact). Only a sealed VerificationManifest
// upgrades an artifact to VERIFIED; exploratory output can never promote —
// it must re-enter through the governed lane (POST /tasks).

export function UnverifiedBadge({ detail }: { detail?: string }) {
  return (
    <div className="rounded-md border-2 border-amber-500 bg-amber-50 px-3 py-2 text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
      <div className="text-sm font-bold tracking-wide">
        UNVERIFIED — kết quả chưa niêm phong
      </div>
      <div className="mt-0.5 text-xs">
        {detail ??
          "Artifact này đến từ lane khám phá: có nguồn trích dẫn nhưng KHÔNG được kiểm chứng end-state. Muốn dùng làm bằng chứng phải chạy lại qua nghiệp vụ governed."}
      </div>
    </div>
  );
}

export function VerifiedSealBadge({ manifestHash }: { manifestHash: string }) {
  return (
    <div className="rounded-md border-2 border-emerald-600 bg-emerald-50 px-3 py-2 text-emerald-900 dark:bg-emerald-950/40 dark:text-emerald-200">
      <div className="text-sm font-bold tracking-wide">
        VERIFIED — đã niêm phong
      </div>
      <div className="mt-0.5 font-mono text-xs break-all">
        manifest_hash: {manifestHash}
      </div>
    </div>
  );
}
