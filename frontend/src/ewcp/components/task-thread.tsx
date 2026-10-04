"use client";

// TaskThread — the governed task surface (spec 002 flow):
// intent box → (clarify | run) → ask-back cards (missing_input file slot,
// option_choice, confirm_value) → progress (step_label) → decision card →
// sealed manifest card. Uploads go through POST /tasks and
// POST /tasks/{id}/inputs — the file ask-back workaround (upstream
// clarification form has no file field; EWCP pane owns the slot instead).

import { useCallback, useEffect, useRef, useState } from "react";

import {
  createTask,
  getRun,
  supplyInputs,
  type RunView,
} from "@/ewcp/api";

import { DecisionCard } from "./decision-card";
import { ManifestCard } from "./manifest-card";

interface Creds {
  apiKey: string;
  tenant: string;
  decidedBy: string;
}

function FileSlot({
  label,
  file,
  onPick,
}: {
  label: string;
  file: File | null;
  onPick: (f: File | null) => void;
}) {
  return (
    <label className="block cursor-pointer rounded-md border border-dashed border-zinc-400 px-3 py-2 text-sm hover:bg-zinc-50 dark:hover:bg-zinc-800">
      <span className="text-zinc-600 dark:text-zinc-300">{label}</span>
      <span className="ml-2 font-mono text-xs text-zinc-400">
        {file ? file.name : "chọn file…"}
      </span>
      <input
        type="file"
        className="hidden"
        onChange={(e) => onPick(e.target.files?.[0] ?? null)}
      />
    </label>
  );
}

export function TaskThread({ creds }: { creds: Creds }) {
  const [intent, setIntent] = useState("");
  const [zip, setZip] = useState<File | null>(null);
  const [books, setBooks] = useState<File | null>(null);
  const [run, setRun] = useState<RunView | null>(null);
  const [clarify, setClarify] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPoll = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = null;
  }, []);

  // poll while the run is moving; stop on terminal/awaiting states
  useEffect(() => {
    stopPoll();
    if (!run) return;
    const active = ![
      "VERIFIED",
      "FAILED",
      "AWAITING_INPUT",
      "REJECTED",
      "CANCELLED",
    ].includes(run.status);
    if (run.status === "CANDIDATE" || !active) return;
    pollRef.current = setInterval(() => {
      void (async () => {
        try {
          setRun(await getRun(run.workrun_id, creds));
        } catch {
          /* transient */
        }
      })();
    }, 2000);
    return stopPoll;
  }, [run, creds, stopPoll]);

  useEffect(() => stopPoll, [stopPoll]);

  async function submit() {
    if (!intent.trim() || busy) return;
    setBusy(true);
    setErr(null);
    setClarify(null);
    try {
      const res = await createTask({
        intent,
        tenant: creds.tenant,
        apiKey: creds.apiKey,
        invoicesZip: zip,
        books,
      });
      if (res.status === "clarify") {
        setClarify(res.clarify_question ?? "Chưa rõ yêu cầu.");
        setRun(null);
      } else {
        setRun(res as RunView);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function sendInputs() {
    if (!run || busy) return;
    setBusy(true);
    setErr(null);
    try {
      setRun(
        await supplyInputs(run.workrun_id, {
          tenant: creds.tenant,
          apiKey: creds.apiKey,
          invoicesZip: zip,
          books,
        }),
      );
      setZip(null);
      setBooks(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const awaitingFiles =
    run?.status === "AWAITING_INPUT" &&
    run.pending_questions.some((q) => q.kind === "missing_input");
  const askBack =
    run?.pending_questions.filter((q) => q.kind !== "missing_input") ?? [];
  const showApproval = run?.status === "CANDIDATE";

  return (
    <div className="space-y-4">
      {/* intake */}
      <div className="rounded-lg border border-zinc-300 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
        <textarea
          value={intent}
          onChange={(e) => setIntent(e.target.value)}
          rows={2}
          placeholder="Yêu cầu nghiệp vụ (tiếng Việt) — vd: đối soát hóa đơn kỳ 09/2025"
          className="w-full resize-y rounded-md border border-zinc-300 bg-transparent p-2 text-sm dark:border-zinc-600"
        />
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          <FileSlot
            label="Zip hóa đơn (XML/PDF)"
            file={zip}
            onPick={setZip}
          />
          <FileSlot label="Sổ kế toán (CSV/XLSX)" file={books} onPick={setBooks} />
        </div>
        <div className="mt-3 flex items-center gap-3">
          {awaitingFiles ? (
            <button
              onClick={sendInputs}
              disabled={busy || (!zip && !books)}
              className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {busy ? "Đang gửi…" : "Gửi file bổ sung"}
            </button>
          ) : (
            <button
              onClick={submit}
              disabled={busy || !intent.trim()}
              className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
            >
              {busy ? "Đang xử lý…" : "Gửi yêu cầu"}
            </button>
          )}
          {run && (
            <span className="text-xs text-zinc-500">
              run <code className="font-mono">{run.workrun_id.slice(0, 8)}…</code>
            </span>
          )}
        </div>
      </div>

      {clarify && (
        <div className="rounded-lg border border-amber-400 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <b>Hệ thống cần rõ hơn:</b> {clarify}
          <div className="mt-1 text-xs opacity-80">
            (Lane khám phá không governed — chỉ nghiệp vụ đã đăng ký mới được niêm phong.)
          </div>
        </div>
      )}

      {err && (
        <div className="rounded-lg border border-red-400 bg-red-50 p-3 text-sm text-red-700 dark:bg-red-950/40 dark:text-red-300">
          {err}
        </div>
      )}

      {run && (
        <div className="space-y-4">
          {/* progress */}
          <div className="rounded-lg border border-zinc-300 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
            <div className="flex items-center justify-between">
              <div className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                Trạng thái
              </div>
              <div
                className={`rounded-full px-2.5 py-0.5 text-xs font-bold ${
                  run.status === "VERIFIED"
                    ? "bg-emerald-100 text-emerald-700"
                    : run.status === "FAILED"
                      ? "bg-red-100 text-red-700"
                      : "bg-blue-100 text-blue-700"
                }`}
              >
                {run.status}
              </div>
            </div>
            <p className="mt-1 text-sm">{run.step_label}</p>
            {run.counts && (
              <p className="mt-1 text-xs text-zinc-500">
                {run.counts.invoices} hóa đơn · {run.counts.book_rows} dòng sổ
              </p>
            )}
            {run.ingest_errors.length > 0 && (
              <details className="mt-2 text-xs">
                <summary className="cursor-pointer text-amber-700">
                  {run.ingest_errors.length} file lỗi đọc
                </summary>
                <ul className="mt-1 list-disc pl-5 text-zinc-500">
                  {run.ingest_errors.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </details>
            )}
            {run.error && (
              <p className="mt-2 text-xs text-red-600">{run.error}</p>
            )}
          </div>

          {/* ask-back cards */}
          {awaitingFiles && (
            <div className="rounded-lg border border-blue-400 bg-blue-50 p-4 text-sm text-blue-900 dark:bg-blue-950/40 dark:text-blue-200">
              <b>Thiếu đầu vào bắt buộc.</b>{" "}
              {run.pending_questions.find((q) => q.kind === "missing_input")
                ?.prompt ?? "Đính kèm file rồi bấm Gửi file bổ sung."}
            </div>
          )}
          {askBack.map((q) => (
            <DecisionCard
              key={q.decision_id}
              q={q}
              run={run}
              creds={{ ...creds }}
              onDone={setRun}
            />
          ))}

          {/* approval gate */}
          {showApproval && (
            <DecisionCard
              q={{
                decision_id: "",
                kind: "approval",
                prompt:
                  "Kết quả đã qua validators — duyệt để niêm phong bằng chứng?",
                options: ["approve", "reject", "request_changes"],
              }}
              run={run}
              creds={{ ...creds }}
              onDone={setRun}
            />
          )}

          {/* deliverables */}
          {run.deliverables.length > 0 && (
            <div className="rounded-lg border border-zinc-300 bg-white p-4 text-sm dark:border-zinc-700 dark:bg-zinc-900">
              <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                Deliverables
              </div>
              <ul className="space-y-1">
                {run.deliverables.map((d) => (
                  <li key={d.deliverable_id} className="font-mono text-xs">
                    {d.name || d.uri} ·{" "}
                    <span className="text-zinc-400">
                      {d.sha256.slice(0, 12)}…
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* sealed manifest */}
          <ManifestCard run={run} creds={creds} />
        </div>
      )}
    </div>
  );
}
