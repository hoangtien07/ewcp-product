"use client";

// /ewcp/verify — public verify surface (boxed route). The API key lives in
// VerifyView's in-memory state: the workbench page persists only
// {tenant, decidedBy} (no token in web storage — repo rule), so nothing
// usable can be restored from sessionStorage here.

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { VerifyView } from "@/ewcp/components/verify-view";

function VerifyInner() {
  const params = useSearchParams();
  return (
    <div className="min-h-screen bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      <div className="mx-auto max-w-2xl px-4 py-8">
        <VerifyView initialRun={params.get("run") ?? undefined} />
      </div>
    </div>
  );
}

export default function EwcpVerifyPage() {
  return (
    <Suspense>
      <VerifyInner />
    </Suspense>
  );
}
