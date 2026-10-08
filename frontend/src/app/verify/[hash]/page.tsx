// Public seal-permalink page — /verify/<manifest_hash>. Third parties
// open the shared link and inspect the sealed evidence through the
// extension's verify proxy (session-authenticated on the product; the
// kernel lookup itself is public by design).

import { use } from "react";

import { VerifyView } from "@/ewcp/components/verify-view";

export default function VerifyPage({
  params,
}: {
  params: Promise<{ hash: string }>;
}) {
  const { hash } = use(params);
  return (
    <div className="mx-auto max-w-3xl p-6">
      <VerifyView initialHash={decodeURIComponent(hash)} />
    </div>
  );
}
