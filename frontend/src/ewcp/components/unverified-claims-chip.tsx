"use client";

// UnverifiedClaimsChip — the F2 honesty marker shown next to the lifecycle
// badge when the run's `integrity_flag` is `claimed_artifacts_missing:*`.
// The run still reports `completed`; the chip marks that the model's
// self-report could not be verified on disk. An empty name list means the
// flag was present but its payload was unreadable — the generic tooltip
// says the same thing without inventing names.

import { missingClaimedArtifacts } from "@/ewcp/labels";

export function UnverifiedClaimsChip({
  integrityFlag,
}: {
  integrityFlag?: string | null;
}) {
  const missing = missingClaimedArtifacts(integrityFlag);
  if (missing === null) return null;
  return (
    <span
      title={
        missing.length > 0
          ? `Không tìm thấy: ${missing.join(", ")}`
          : "Kết quả tự báo cáo không xác minh được trên đĩa"
      }
      className="rounded border border-red-500/50 bg-red-50 px-1.5 py-0.5 text-[10px] font-semibold text-red-700 dark:bg-red-950/40 dark:text-red-300"
    >
      Tự báo cáo — chưa xác minh
    </span>
  );
}
