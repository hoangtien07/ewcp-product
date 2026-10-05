"use client";

// /ewcp — the governed workspace pane (EWCP studio surface). New boxed route;
// zero upstream file edits. API calls ride the existing /api/:path* rewrite →
// Gateway /api/ewcp/* → in-process kernel.

import { useCallback, useEffect, useState } from "react";

import { getRun, type RunView } from "@/ewcp/api";
import { TaskList } from "@/ewcp/components/task-list";
import { TaskThread } from "@/ewcp/components/task-thread";
import { UnverifiedBadge } from "@/ewcp/components/unverified-badge";

const CREDS_KEY = "ewcp_creds";
const LAST_RUN_KEY = "ewcp_last_run";

export default function EwcpPage() {
  // apiKey stays in memory only (repo rule: no tokens in web storage);
  // tenant/decidedBy are non-secret prefs and may persist.
  const [creds, setCreds] = useState({
    apiKey: "",
    tenant: "demo",
    decidedBy: "nguoi.duyet",
  });
  const [run, setRun] = useState<RunView | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  // Single writer for the active run: also persists the id (reload restore)
  // and bumps refreshKey so the history rail reflects the new state.
  const updateRun = useCallback((r: RunView | null) => {
    setRun(r);
    setRefreshKey((k) => k + 1);
    try {
      if (r?.workrun_id) {
        sessionStorage.setItem(LAST_RUN_KEY, r.workrun_id);
      } else {
        sessionStorage.removeItem(LAST_RUN_KEY);
      }
    } catch {
      /* private mode */
    }
  }, []);

  // restore the last open run on mount — a pending ask-back or approval
  // must stay reachable across reloads
  useEffect(() => {
    let id: string | null = null;
    try {
      id = sessionStorage.getItem(LAST_RUN_KEY);
    } catch {
      /* private mode */
    }
    if (!id) return;
    void getRun(id, { apiKey: "" })
      .then((r) => updateRun(r))
      .catch(() => {
        try {
          sessionStorage.removeItem(LAST_RUN_KEY);
        } catch {
          /* ignore */
        }
      });
    // mount-only restore — history clicks go through select() instead
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const select = useCallback(
    (workrunId: string) => {
      void getRun(workrunId, { apiKey: creds.apiKey })
        .then((r) => updateRun(r))
        .catch(() => {
          /* stale row — leave current view */
        });
    },
    [creds.apiKey, updateRun],
  );

  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(CREDS_KEY);
      if (raw) {
        const saved = JSON.parse(raw) as Partial<typeof creds>;
        setCreds((c) => ({
          ...c,
          tenant: saved.tenant ?? c.tenant,
          decidedBy: saved.decidedBy ?? c.decidedBy,
        }));
      }
    } catch {
      /* fresh session */
    }
  }, []);

  function update(patch: Partial<typeof creds>) {
    setCreds((c) => {
      const next = { ...c, ...patch };
      try {
        sessionStorage.setItem(
          CREDS_KEY,
          JSON.stringify({ tenant: next.tenant, decidedBy: next.decidedBy }),
        );
      } catch {
        /* private mode */
      }
      return next;
    });
  }

  return (
    <div className="min-h-screen bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
      <div className="mx-auto flex max-w-6xl gap-6 px-4 py-8">
        {/* history rail — governed workbench, not a one-shot form */}
        <aside className="w-72 shrink-0 space-y-3">
          <button
            type="button"
            onClick={() => updateRun(null)}
            className="w-full rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm font-medium hover:bg-zinc-50 dark:border-zinc-700 dark:bg-zinc-900 dark:hover:bg-zinc-800"
          >
            + Yêu cầu mới
          </button>
          <div className="space-y-2">
            <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
              Lịch sử yêu cầu
            </h2>
            <TaskList
              creds={creds}
              activeId={run?.workrun_id ?? null}
              refreshKey={refreshKey}
              onSelect={select}
            />
          </div>
        </aside>

        <div className="min-w-0 flex-1 space-y-5">
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

        <TaskThread creds={creds} run={run} onRun={updateRun} />

        {/* exploratory-lane demo: UNVERIFIED chrome is dominant by rule */}
        <section className="space-y-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
            Lane khám phá (không governed)
          </h2>
          <UnverifiedBadge />
        </section>
        </div>
      </div>
    </div>
  );
}
