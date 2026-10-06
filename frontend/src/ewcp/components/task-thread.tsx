"use client";

// TaskThread — the governed task surface (spec 002 flow):
// intent box → (clarify | run) → ask-back cards (missing_input file slot,
// option_choice, confirm_value) → progress (step_label) → decision card →
// sealed manifest card. Uploads go through POST /tasks and
// POST /tasks/{id}/inputs — the file ask-back workaround (upstream
// clarification form has no file field; EWCP pane owns the slot instead).

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  createTask,
  downloadDeliverable,
  fetchDemoFixture,
  getRun,
  listOutcomes,
  supplyInputs,
  type OutcomeSpecView,
  type RunView,
} from "@/ewcp/api";
import {
  handoffThreadPath,
  handoffToGeneralLane,
  type ExploratoryHandoff,
} from "@/ewcp/exploratory";
import {
  deliverableKindLabel,
  formatCounts,
  outcomeLabel,
  statusLabel,
} from "@/ewcp/labels";
import {
  bindPresets,
  FALLBACK_SPECS,
  inputsFor,
  isZipInput,
  type BoundPreset,
} from "@/ewcp/registry";

import { DecisionCard } from "./decision-card";
import { GeneralCard } from "./general-card";
import { ManifestCard } from "./manifest-card";
import { PackGallery } from "./pack-gallery";
import { StudioCard } from "./studio-card";

interface Creds {
  apiKey: string;
  tenant: string;
  decidedBy: string;
}

function FileSlot({
  label,
  accept,
  file,
  onPick,
}: {
  label: string;
  accept?: string;
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
        accept={accept}
        className="hidden"
        onChange={(e) => onPick(e.target.files?.[0] ?? null)}
      />
    </label>
  );
}

export function TaskThread({
  creds,
  run,
  onRun,
  onHandoff,
}: {
  creds: Creds;
  run: RunView | null;
  onRun: (r: RunView | null) => void;
  onHandoff?: (h: ExploratoryHandoff) => void;
}) {
  const router = useRouter();
  const [intent, setIntent] = useState("");
  // file slots keyed by the spec's declared multipart field names
  // (requires_inputs[].name) — the registry decides which exist
  const [files, setFiles] = useState<Record<string, File | null>>({});
  // pack registry from GET /outcomes — FALLBACK_SPECS keeps the pane
  // working against kernels that predate the registry endpoint
  const [specs, setSpecs] = useState<OutcomeSpecView[]>(FALLBACK_SPECS);
  // true once GET /outcomes answered — until then the pane runs on the
  // static fallback and surfaces a notice instead of silently
  // degrading (a wrong/expired key otherwise looks like missing packs)
  const [specsLive, setSpecsLive] = useState(false);
  const [clarify, setClarify] = useState<string | null>(null);
  // kernel clarify payload flag (spec 005 AC1): false = no governed
  // general lane on this kernel -> exploratory handoff stays the only
  // route; true = the lane exists and the kernel will route unmatched
  // intents itself, so the card's note explains this clarify instead
  const [clarifyAssist, setClarifyAssist] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // gallery starter chips fill + focus this box
  const intentRef = useRef<HTMLTextAreaElement | null>(null);
  // Idempotency-Key per draft: one key for the same (intent + files)
  // submission, so a retry after a network error replays server-side
  // instead of creating a second run. A different draft gets a new key.
  const idemRef = useRef<{ fp: string; key: string } | null>(null);

  // run state + restore live in the page (history rail selects there too);
  // this component only drives the active run forward.
  const stopPoll = useCallback(() => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = null;
  }, []);

  // poll while the run is moving; stop on terminal/awaiting states.
  // Kernel TaskStatus values are lowercase enum values.
  useEffect(() => {
    stopPoll();
    if (!run) return;
    const active = ![
      "verified",
      "failed",
      "cancelled",
      "rejected",
      "awaiting_input",
      "awaiting_approval",
      "candidate_complete",
    ].includes(run.status);
    if (!active) return;
    pollRef.current = setInterval(() => {
      void (async () => {
        try {
          onRun(await getRun(run.workrun_id, creds));
        } catch {
          /* transient */
        }
      })();
    }, 2000);
    return stopPoll;
  }, [run, creds, stopPoll, onRun]);

  useEffect(() => stopPoll, [stopPoll]);

  // fetch the registry once per api-key change — in keyed deployments a
  // 401 means the key isn't right yet, and re-running on change retries
  // it; any failure keeps the fallback copy (pane stays usable)
  useEffect(() => {
    let dead = false;
    void listOutcomes({ apiKey: creds.apiKey })
      .then((s) => {
        if (!dead && Array.isArray(s) && s.length > 0) {
          setSpecs(s);
          setSpecsLive(true);
        }
      })
      .catch(() => {
        /* pre-registry kernel, offline, or a bad key — drop back to the
           static copy so the notice reflects the CURRENT state rather
           than stale live specs */
        if (!dead) {
          setSpecs(FALLBACK_SPECS);
          setSpecsLive(false);
        }
      });
    return () => {
      dead = true;
    };
  }, [creds.apiKey]);

  // a selected run narrows the slots to its own outcome's inputs; no run
  // means intake can be any pack, so the union of declared slots shows
  const slots = inputsFor(specs, run?.outcome_type);
  const presets = bindPresets(specs);
  // spec-driven routing hint: first preset's sample intent, else the
  // first spec's description — never a hardcoded pack example
  const intentHint = presets[0]?.intent ?? specs[0]?.description;

  function pickFile(name: string, f: File | null) {
    setFiles((cur) => {
      const next = { ...cur, [name]: f };
      const input = slots.find((s) => s.name === name);
      // kernel accepts at most one zip per request — picking a zip slot
      // clears the other zip slots (they belong to different packs)
      if (f && input && isZipInput(input)) {
        for (const s of slots) {
          if (s.name !== name && isZipInput(s)) next[s.name] = null;
        }
      }
      return next;
    });
  }

  async function submit() {
    if (!intent.trim() || busy) return;
    setBusy(true);
    setErr(null);
    setClarify(null);
    try {
      // fingerprint covers name+size+mtime so a re-picked file with the
      // same name but different content counts as a new submission
      const fp = [intent, ...slots.map((s) => files[s.name])]
        .map((x) =>
          x instanceof File ? `${x.name}:${x.size}:${x.lastModified}` : x,
        )
        .join("|");
      if (idemRef.current?.fp !== fp) {
        idemRef.current = { fp, key: crypto.randomUUID() };
      }
      const res = await createTask({
        intent,
        tenant: creds.tenant,
        apiKey: creds.apiKey,
        files,
        idempotencyKey: idemRef.current.key,
      });
      if (res.status === "clarify") {
        setClarify(res.clarify_question ?? "Chưa rõ yêu cầu.");
        setClarifyAssist(res.assist_available === true);
        onRun(null);
      } else {
        onRun(res as RunView);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  // Clarify dead-end → handoff: re-intake the draft intent into the
  // general assistant lane (upstream lead_agent chat). Q24 firewall —
  // re-intake only; the handed-off thread stays UNVERIFIED forever.
  async function handoff() {
    if (!clarify || busy) return;
    setBusy(true);
    setErr(null);
    try {
      const h = await handoffToGeneralLane({
        intent: intent.trim() || clarify,
        clarifyQuestion: clarify,
      });
      onHandoff?.(h);
      router.push(handoffThreadPath(h.thread_id));
      // busy stays set — the router swap unmounts this pane
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  async function sendInputs() {
    if (!run || busy) return;
    setBusy(true);
    setErr(null);
    try {
      onRun(
        await supplyInputs(run.workrun_id, {
          tenant: creds.tenant,
          apiKey: creds.apiKey,
          files,
        }),
      );
      setFiles({});
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function loadPreset(preset: BoundPreset) {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      // a preset is the whole draft — it replaces the file map wholesale
      // so stale picks from a previous workflow never ride along
      const declared = new Set(preset.inputs);
      const next: Record<string, File | null> = {};
      for (const [name, fixture] of Object.entries(preset.fixtures)) {
        if (declared.has(name)) {
          next[name] = await fetchDemoFixture(fixture);
        }
      }
      setFiles(next);
      if (!intent.trim()) setIntent(preset.intent);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const awaitingFiles =
    run?.status === "awaiting_input" &&
    run.pending_questions.some((q) => q.kind === "missing_input");
  const askBack =
    run?.pending_questions.filter((q) => q.kind !== "missing_input") ?? [];
  // candidate_complete = attempt signaled done awaiting seal decision;
  // awaiting_approval is the kernel's equivalent pre-approval state — both
  // need the approval card or the run wedges with no control left.
  const showApproval =
    run?.status === "candidate_complete" || run?.status === "awaiting_approval";
  // kernel 409s an approval decision while pending_questions are still open —
  // keep the gate visible but inert until every question is answered
  const questionsOpen = (run?.pending_questions.length ?? 0) > 0;

  return (
    <div className="space-y-4">
      {/* intake */}
      <div className="rounded-lg border border-zinc-300 bg-white p-4 dark:border-zinc-700 dark:bg-zinc-900">
        <textarea
          ref={intentRef}
          value={intent}
          onChange={(e) => setIntent(e.target.value)}
          rows={2}
          placeholder={`Yêu cầu nghiệp vụ (tiếng Việt) — vd: ${intentHint ?? "đối soát hóa đơn"}`}
          className="w-full resize-y rounded-md border border-zinc-300 bg-transparent p-2 text-sm dark:border-zinc-600"
        />
        {/* router hint — what the registered packs understand */}
        <p className="mt-1 text-xs text-zinc-400">
          Router nhận:{" "}
          {specs.map((s) => (
            <span
              key={s.outcome_type}
              title={s.description}
              className="mr-2 inline-block"
            >
              {outcomeLabel(s.outcome_type)}
            </span>
          ))}
        </p>
        {!specsLive && (
          <p className="mt-1 text-xs text-amber-600 dark:text-amber-400">
            Spec tĩnh — không đọc được registry kernel (kiểm tra API key); một
            số nghiệp vụ có thể thiếu.
          </p>
        )}
        <div className="mt-2 grid gap-2 sm:grid-cols-3">
          {slots.map((s) => (
            <FileSlot
              key={s.name}
              label={s.required ? s.label_vn : `${s.label_vn} (tuỳ chọn)`}
              accept={s.accept}
              file={files[s.name] ?? null}
              onPick={(f) => pickFile(s.name, f)}
            />
          ))}
        </div>
        {presets.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
            <span className="text-zinc-500">Dữ liệu mẫu:</span>
            {presets.map((p) => (
              <button
                key={`${p.outcomeType}:${p.label}`}
                type="button"
                disabled={busy}
                onClick={() => void loadPreset(p)}
                className="rounded border border-zinc-300 px-2 py-0.5 text-zinc-600 hover:bg-zinc-50 disabled:opacity-50 dark:border-zinc-600 dark:text-zinc-300 dark:hover:bg-zinc-800"
              >
                {p.label}
              </button>
            ))}
          </div>
        )}
        <div className="mt-3 flex items-center gap-3">
          {awaitingFiles ? (
            <button
              onClick={sendInputs}
              disabled={busy || !Object.values(files).some(Boolean)}
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
              run{" "}
              <code className="font-mono">{run.workrun_id.slice(0, 8)}…</code>
            </span>
          )}
        </div>
      </div>

      {/* pack gallery — catalog browse surface for a fresh request
          (gap-catalog Catalog UX #2). Hidden while a run is open: the
          intake slots then narrow to that run's own spec, and the run
          surface is the focus. */}
      {!run && (
        <PackGallery
          specs={specs}
          presets={presets}
          busy={busy}
          onIntent={(t) => {
            setIntent(t);
            intentRef.current?.focus();
          }}
          onPreset={(p) => void loadPreset(p)}
        />
      )}

      {clarify && (
        <div className="rounded-lg border border-amber-400 bg-amber-50 p-3 text-sm text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          <b>Hệ thống cần rõ hơn:</b> {clarify}
          <div className="mt-1 text-xs opacity-80">
            {clarifyAssist
              ? "(Kernel đã có lane tổng quát governed đã sẵn sàng — yêu cầu nêu rõ đầu ra mong muốn sẽ được route trực tiếp. Handoff khám phá vẫn không niêm phong.)"
              : "(Lane khám phá không governed — chỉ nghiệp vụ đã đăng ký mới được niêm phong.)"}
          </div>
          <button
            type="button"
            onClick={() => void handoff()}
            disabled={busy}
            className="mt-2 rounded-md border border-amber-600 bg-white px-3 py-1 text-xs font-medium text-amber-900 hover:bg-amber-100 disabled:opacity-50 dark:bg-transparent dark:text-amber-200 dark:hover:bg-amber-900/40"
          >
            {busy ? "Đang chuyển…" : "Chuyển sang trợ lý tổng quát"}
          </button>
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
              <div className="text-xs font-semibold tracking-wide text-zinc-500 uppercase">
                Trạng thái
              </div>
              <div
                className={`rounded-full px-2.5 py-0.5 text-xs font-bold ${
                  run.status === "verified"
                    ? "bg-emerald-100 text-emerald-700"
                    : ["failed", "rejected", "cancelled"].includes(run.status)
                      ? "bg-red-100 text-red-700"
                      : "bg-blue-100 text-blue-700"
                }`}
              >
                {statusLabel(run.status)}
              </div>
            </div>
            <p className="mt-1 text-sm">{run.step_label}</p>
            {run.counts && (
              <p className="mt-1 text-xs text-zinc-500">
                {formatCounts(run.counts)}
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
              onDone={onRun}
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
              onDone={onRun}
              disabledHint={
                questionsOpen ? "Trả lời hết câu hỏi trước" : undefined
              }
            />
          )}

          {/* deliverables */}
          {run.deliverables.length > 0 && (
            <div className="rounded-lg border border-zinc-300 bg-white p-4 text-sm dark:border-zinc-700 dark:bg-zinc-900">
              <div className="mb-2 text-xs font-semibold tracking-wide text-zinc-500 uppercase">
                Deliverables
              </div>
              <ul className="space-y-1">
                {run.deliverables.map((d) => (
                  <li
                    key={d.deliverable_id}
                    className="flex items-center gap-2 font-mono text-xs"
                  >
                    <span className="flex-1">
                      {d.name || d.uri}{" "}
                      <span className="rounded bg-zinc-100 px-1.5 text-[10px] text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
                        {deliverableKindLabel(d.kind)}
                      </span>{" "}
                      <span className="text-zinc-400">
                        {d.sha256.slice(0, 12)}…
                      </span>
                    </span>
                    <button
                      type="button"
                      onClick={() =>
                        void downloadDeliverable(
                          run.workrun_id,
                          d.deliverable_id,
                          {
                            apiKey: creds.apiKey,
                            name: d.name || d.deliverable_id,
                          },
                        ).catch((e: unknown) =>
                          setErr(e instanceof Error ? e.message : String(e)),
                        )
                      }
                      className="rounded border border-zinc-300 px-2 py-0.5 text-zinc-600 hover:bg-zinc-50 dark:border-zinc-600 dark:text-zinc-300 dark:hover:bg-zinc-800"
                    >
                      Tải
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* general lane — TaskState/contract projection + outcome JSON */}
          {run.outcome_type === "general" && (
            <GeneralCard run={run} apiKey={creds.apiKey} />
          )}

          {/* studio surface — outcome-rich view fed by /outcome */}
          <StudioCard run={run} apiKey={creds.apiKey} />

          {/* sealed manifest */}
          <ManifestCard run={run} creds={creds} />
        </div>
      )}
    </div>
  );
}
