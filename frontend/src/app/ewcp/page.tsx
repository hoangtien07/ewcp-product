"use client";

// /ewcp — the governed workspace pane (EWCP studio surface). New boxed route;
// zero upstream file edits. API calls ride the existing /api/:path* rewrite →
// Gateway /api/ewcp/* → in-process kernel.

import { useEffect, useState } from "react";

import { TaskThread } from "@/ewcp/components/task-thread";
import { UnverifiedBadge } from "@/ewcp/components/unverified-badge";

const CREDS_KEY = "ewcp_creds";

export default function EwcpPage() {
  const [creds, setCreds] = useState({
    apiKey: "",
    tenant: "demo",
    decidedBy: "nguoi.duyet",
  });

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(CREDS_KEY);
      if (raw) setCreds((c) => ({ ...c, ...JSON.parse(raw) }));
    } catch {
      /* fresh session */
    }
  }, []);

  function update(patch: Partial<typeof creds>) {
    setCreds((c) => {
      const next = { ...c, ...patch };
      try {
        sessionStorage.setItem(CREDS_KEY, JSON.stringify(next));
      } catch {
        /* private mode */
      }
      return next;
    });
  }

  return (
    <div className="min-h-screen bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      <div className="mx-auto max-w-2xl space-y-5 px-4 py-8">
        <header className="space-y-1">
          <h1 className="text-xl font-bold">EWCP — Nghiệp vụ governed</h1>
          <p className="text-sm text-zinc-500">
            Gửi yêu cầu — hệ thống hoàn thành việc, hỏi lại khi thiếu đầu vào,
            và niêm phong kết quả thành bằng chứng kiểm chứng được.
          </p>
        </header>

        {/* credentials strip */}
        <div className="grid gap-2 rounded-lg border border-zinc-300 bg-white p-3 text-xs dark:border-zinc-700 dark:bg-zinc-900 sm:grid-cols-3">
          <label className="space-y-0.5">
            <span className="text-zinc-500">API key tenant</span>
            <input
              type="password"
              value={creds.apiKey}
              onChange={(e) => update({ apiKey: e.target.value })}
              placeholder="để trống = dev mode"
              className="w-full rounded border border-zinc-300 bg-transparent px-2 py-1 dark:border-zinc-600"
            />
          </label>
          <label className="space-y-0.5">
            <span className="text-zinc-500">Tenant</span>
            <input
              value={creds.tenant}
              onChange={(e) => update({ tenant: e.target.value })}
              className="w-full rounded border border-zinc-300 bg-transparent px-2 py-1 dark:border-zinc-600"
            />
          </label>
          <label className="space-y-0.5">
            <span className="text-zinc-500">Người duyệt (ghi vào quyết định)</span>
            <input
              value={creds.decidedBy}
              onChange={(e) => update({ decidedBy: e.target.value })}
              className="w-full rounded border border-zinc-300 bg-transparent px-2 py-1 dark:border-zinc-600"
            />
          </label>
        </div>

        <TaskThread creds={creds} />

        {/* exploratory-lane demo: UNVERIFIED chrome is dominant by rule */}
        <section className="space-y-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
            Lane khám phá (không governed)
          </h2>
          <UnverifiedBadge />
        </section>
      </div>
    </div>
  );
}
