"use client";

// /ewcp/verify — public verify surface (boxed route).

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { VerifyView } from "@/ewcp/components/verify-view";

const CREDS_KEY = "ewcp_creds";

function VerifyInner() {
  const params = useSearchParams();
  const [apiKey, setApiKey] = useState("");
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(CREDS_KEY);
      if (raw) setApiKey(JSON.parse(raw).apiKey ?? "");
    } catch {
      /* ignore */
    }
  }, []);
  return (
    <div className="min-h-screen bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      <div className="mx-auto max-w-2xl px-4 py-8">
        <VerifyView
          initialRun={params.get("run") ?? undefined}
          creds={{ apiKey }}
        />
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
