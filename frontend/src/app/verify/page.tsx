// Public verify root — /verify. The verify-by-them lane gets a stable
// entry independent of a prior share link (A6 proposal #10): the same
// VerifyView the permalink page mounts, minus the seeded hash — the
// hash-lookup form doubles as the permalink entry point.

import { VerifyView } from "@/ewcp/components/verify-view";

export default function VerifyRootPage() {
  return (
    <div className="mx-auto max-w-3xl p-6">
      <VerifyView />
    </div>
  );
}
