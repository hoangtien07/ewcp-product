// Public seal-permalink page — /verify/<manifest_hash>. Third parties
// open the shared link and inspect the sealed evidence through the
// extension's public verify proxy: the manifest hash itself is the
// capability, same contract as the kernel's public /verify routes.

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
