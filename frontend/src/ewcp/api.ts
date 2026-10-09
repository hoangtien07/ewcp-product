// EWCP ExecutionRun surface API — talks to /api/ewcp/* (Gateway extension
// routes) with the product's session auth. The kernel key is M2M-only:
// the browser never sees it; governed reads/decisions ride the extension's
// server-side KernelClient.
//
// Observation is the Gateway SSE contract directly: `run.join_url` is the
// read-only GET /api/threads/{tid}/runs/{rid}/join endpoint (T2 launcher
// constants) — `joinRunStream` parses its SSE frames; no polling.

import { fetch } from "@/core/api/fetcher";

const API = "/api/ewcp";

// ---------------------------------------------------------------------------
// ExecutionRunMap rows (extension view — owner-scoped server-side)
// ---------------------------------------------------------------------------

export type ExecutionRunStatus =
  | "launching"
  | "running"
  | "pending_interrupt"
  | "completed"
  | "failed"
  | "timeout"
  | "interrupted";

export interface ExecutionRun {
  execution_run_id: string;
  thread_id: string;
  run_id: string | null;
  workrun_id: string | null;
  task_mode: "general" | "governed";
  status: ExecutionRunStatus;
  intent: string;
  idempotency_key: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  /** Gateway SSE join path — null until the run is admitted. */
  join_url: string | null;
}

export interface EwcpIdentity {
  user_id: string;
  /** `user:<id>` — what the extension asserts on X-Ewcp-Actor. */
  actor: string;
  /** Whether decision actions may bind the product user as the audit
   * principal (kernel X-Ewcp-Actor contract). false → decisions render
   * read-only; POST /runs/{id}/decisions 409s. */
  user_actor_binding: boolean;
}

// ---------------------------------------------------------------------------
// Kernel wire shapes (unchanged — the extension proxies these verbatim)
// ---------------------------------------------------------------------------

export interface DecisionOption {
  id: string; // stable slug — the dispatch key clients post back
  label: string; // display text (kernel already sends Vietnamese prose)
}

export interface PendingQuestion {
  decision_id: string;
  kind: string; // missing_input | option_choice | confirm_value | approval
  prompt: string;
  options: string[]; // legacy wire: labels a pane may post verbatim
  options_v2?: DecisionOption[]; // slug contract — post .id when present
}

export interface Deliverable {
  deliverable_id: string;
  kind: string;
  sha256: string;
  uri: string;
  name: string;
}

export interface RunView {
  workrun_id: string;
  tenant_id: string;
  outcome_type: string;
  status: string; // lowercase kernel TaskStatus enums
  step_label: string;
  intent: string;
  thread_id?: string;
  pending_questions: PendingQuestion[];
  deliverables: Deliverable[];
  ingest_errors: string[];
  skipped: string[];
  counts?: Record<string, number | undefined>;
  general?: Record<string, unknown>;
  spend?: {
    tokens?: number;
    usd?: number;
    calls?: number;
    cap_usd?: number;
  };
  context?: Record<string, unknown>;
  decision?: { answer: string; decided_by: string };
  error?: string;
}

export interface VerifyResult {
  workrun_id: string;
  verdict: "PASS" | "FAIL";
  manifest_ok: boolean;
  artifacts: {
    deliverable_id: string;
    name: string;
    expected_sha256: string;
    got_sha256: string | null;
    ok: boolean;
    missing: boolean;
  }[];
}

export interface OutcomeInputSpec {
  name: string; // multipart field name
  accept: string; // file-picker accept filter
  label_vn: string; // Vietnamese slot label
  required: boolean;
}

export interface OutcomeSpecView {
  outcome_type: string;
  description: string;
  required_checks: string[];
  requires_inputs: OutcomeInputSpec[];
}

export interface PermalinkDeliverable {
  deliverable_id?: string;
  kind?: string;
  sha256?: string;
  name?: string;
}

export interface PermalinkCheck {
  name?: string;
  result?: string;
  detail?: string;
  validator_ref?: string;
  evidence_hash?: string;
}

export interface VerifyPermalinkResult {
  workrun_id: string;
  // true = seal recomputes | false = mismatch | null = kernel has no seal
  // key configured (authenticity unprovable, manifest still resolves)
  seal_ok: boolean | null;
  manifest: {
    workrun_id?: string;
    manifest_hash?: string;
    seal?: string | null;
    deliverables?: PermalinkDeliverable[];
    checks?: PermalinkCheck[];
  };
}

export class EwcpError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

async function parse<T>(res: Response): Promise<T> {
  const text = await res.text();
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    body = { detail: text };
  }
  if (!res.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? String((body as { detail: unknown }).detail)
        : text;
    throw new EwcpError(res.status, detail);
  }
  return body as T;
}

// ---------------------------------------------------------------------------
// Calls
// ---------------------------------------------------------------------------

export async function getIdentity(): Promise<EwcpIdentity> {
  return parse(await fetch(`${API}/identity`));
}

export async function listExecutionRuns(): Promise<ExecutionRun[]> {
  const body = await parse<{ runs: ExecutionRun[] }>(
    await fetch(`${API}/runs`),
  );
  return body.runs;
}

/** Chat-page reverse lookup — which ExecutionRuns ride this thread.
 * Server-side this is a map-store read (no foreground recovery). */
export async function listRunsForThread(
  threadId: string,
): Promise<ExecutionRun[]> {
  const body = await parse<{ runs: ExecutionRun[] }>(
    await fetch(`${API}/runs?thread_id=${encodeURIComponent(threadId)}`),
  );
  return body.runs;
}

/** Submit an interrupt response for a run parked at `pending_interrupt`
 * (the lead_agent's ask_human/clarification). `resume` is the raw JSON
 * payload forwarded to AgentRuns.resume — for the agent's clarification
 * questions the contract is a plain string answer. */
export async function resumeRun(
  executionRunId: string,
  resume: unknown,
): Promise<ExecutionRun> {
  return parse(
    await fetch(`${API}/runs/${encodeURIComponent(executionRunId)}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resume }),
    }),
  );
}

export async function getExecutionRun(
  executionRunId: string,
  opts?: { refresh?: boolean },
): Promise<ExecutionRun> {
  const q = opts?.refresh ? "?refresh=1" : "";
  const body = await parse<{ run: ExecutionRun }>(
    await fetch(`${API}/runs/${encodeURIComponent(executionRunId)}${q}`),
  );
  return body.run;
}

export async function launchExecutionRun(args: {
  intent: string;
  taskMode?: "general" | "governed";
  workrunId?: string;
  idempotencyKey?: string;
  /** Governed dispatch: explicit pack — POSTs kernel
   * `/outcomes/{type}/run` instead of router intake. */
  outcomeType?: string;
  /** Dev-mode kernels only: tenant carried as a form field (keyed
   * kernels derive the tenant from the key and ignore this). */
  tenantId?: string;
  /** Thread attachments — general lane only (`files` field). */
  files?: File[];
  /** Spec-declared context keys (mst, ky…) — plain form fields. */
  fields?: Record<string, string>;
  /** Governed slot uploads: field name per spec input (invoices_zip,
   * books, …) — forwarded to kernel intake, NOT thread attachments. */
  slotFiles?: Record<string, File[]>;
}): Promise<{ run: ExecutionRun; idempotent_replay: boolean }> {
  const fd = new FormData();
  fd.set("intent", args.intent);
  if (args.taskMode) fd.set("task_mode", args.taskMode);
  if (args.workrunId) fd.set("workrun_id", args.workrunId);
  if (args.idempotencyKey) fd.set("idempotency_key", args.idempotencyKey);
  if (args.outcomeType) fd.set("outcome_type", args.outcomeType);
  if (args.tenantId) fd.set("tenant_id", args.tenantId);
  for (const [name, value] of Object.entries(args.fields ?? {})) {
    fd.set(name, value);
  }
  for (const f of args.files ?? []) fd.append("files", f);
  for (const [slot, members] of Object.entries(args.slotFiles ?? {})) {
    for (const f of members) fd.append(slot, f);
  }
  return parse(await fetch(`${API}/runs`, { method: "POST", body: fd }));
}

// Governed (kernel) reads — keyed by ExecutionRun so the extension
// enforces owner-scoping; each 404s when the run has no workrun binding.

export async function getWorkrun(executionRunId: string): Promise<RunView> {
  const body = await parse<{ workrun: RunView }>(
    await fetch(`${API}/runs/${encodeURIComponent(executionRunId)}/workrun`),
  );
  return body.workrun;
}

export async function getManifest(
  executionRunId: string,
): Promise<Record<string, unknown>> {
  return parse(
    await fetch(`${API}/runs/${encodeURIComponent(executionRunId)}/manifest`),
  );
}

export async function getOutcome(
  executionRunId: string,
): Promise<{ outcome_type: string; result: unknown }> {
  return parse(
    await fetch(`${API}/runs/${encodeURIComponent(executionRunId)}/outcome`),
  );
}

export async function getEvidence(
  executionRunId: string,
): Promise<Record<string, unknown>> {
  return parse(
    await fetch(`${API}/runs/${encodeURIComponent(executionRunId)}/evidence`),
  );
}

export async function submitDecision(
  executionRunId: string,
  args: { answer: string; decisionId?: string },
): Promise<RunView> {
  return parse(
    await fetch(`${API}/runs/${encodeURIComponent(executionRunId)}/decisions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        answer: args.answer,
        decision_id: args.decisionId ?? null,
      }),
    }),
  );
}

export async function downloadDeliverable(
  executionRunId: string,
  deliverableId: string,
  args?: { name?: string },
): Promise<void> {
  const res = await fetch(
    `${API}/runs/${encodeURIComponent(executionRunId)}/deliverables/${encodeURIComponent(deliverableId)}`,
  );
  if (!res.ok) throw new EwcpError(res.status, await res.text());
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = args?.name ?? deliverableId;
  a.click();
  URL.revokeObjectURL(url);
}

export async function listOutcomes(): Promise<OutcomeSpecView[]> {
  const body = await parse<{ outcomes: OutcomeSpecView[] }>(
    await fetch(`${API}/outcomes`),
  );
  return body.outcomes;
}

export async function verifyArtifacts(args: {
  evidenceJson: File;
  files: File[];
}): Promise<VerifyResult> {
  const fd = new FormData();
  fd.set("evidence_json", args.evidenceJson);
  for (const f of args.files) fd.append("files", f);
  return parse(await fetch(`${API}/verify`, { method: "POST", body: fd }));
}

// PUBLIC seal permalink — anonymous callers allowed: the manifest hash
// is the capability (kernel contract, mirrored by the extension proxy).
export async function verifyPermalink(
  manifestHash: string,
): Promise<VerifyPermalinkResult> {
  return parse(
    await fetch(`${API}/verify/${encodeURIComponent(manifestHash)}`),
  );
}

// The shareable URL a third party opens — the public verify page under
// /verify/[hash] reads it back through the extension proxy.
export function verifyShareUrl(manifestHash: string): string {
  const origin = typeof window !== "undefined" ? window.location.origin : "";
  return `${origin}/verify/${encodeURIComponent(manifestHash)}`;
}

// ---------------------------------------------------------------------------
// SSE observation — GET join on the run's stream (read-only;
// apply_on_disconnect=False server-side, so closing never cancels work)
// ---------------------------------------------------------------------------

export interface RunStreamEvent {
  /** SSE `event:` name ("values", "messages-tuple", "error", "end", …). */
  event: string;
  /** Decoded JSON payload of the `data:` frame (raw string when non-JSON). */
  data: unknown;
}

export async function joinRunStream(
  joinUrl: string,
  handlers: {
    onEvent?: (event: RunStreamEvent) => void;
    signal?: AbortSignal;
  } = {},
): Promise<void> {
  const res = await fetch(joinUrl, {
    headers: { Accept: "text/event-stream" },
    signal: handlers.signal ?? null,
  });
  if (!res.ok) throw new EwcpError(res.status, await res.text());
  const body = res.body;
  if (!body) throw new EwcpError(0, "run stream has no body");
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let event = "message";
  let dataLines: string[] = [];
  const flush = () => {
    if (dataLines.length === 0) return;
    const raw = dataLines.join("\n");
    dataLines = [];
    let data: unknown = raw;
    try {
      data = JSON.parse(raw);
    } catch {
      // keep the raw string
    }
    handlers.onEvent?.({ event, data });
    event = "message";
  };
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (line === "") {
          flush();
        } else if (line.startsWith("event:")) {
          event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        }
      }
    }
    buffer += decoder.decode();
    for (const line of buffer.split("\n")) {
      if (line === "") {
        flush();
      } else if (line.startsWith("event:")) {
        event = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    flush();
  } finally {
    reader.releaseLock();
  }
}
