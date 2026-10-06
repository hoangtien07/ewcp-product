// EWCP kernel API client — talks to /api/ewcp/* (Next rewrite → Gateway →
// extension router → in-process kernel mount). Auth: x-ewcp-api-key when the
// tenant entered a key, else tenant_id form field (dev/demo mode).

export interface DecisionOption {
  id: string; // stable slug — the dispatch key new clients post back
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
  status: string; // AWAITING_INPUT | RUNNING | CANDIDATE | VERIFIED | FAILED | ...
  step_label: string;
  intent: string;
  thread_id?: string;
  pending_questions: PendingQuestion[];
  deliverables: Deliverable[];
  ingest_errors: string[];
  skipped: string[];
  // pack-owned counters from spec.summarize (run_summary.json) — the
  // pane renders them generically, keyed by count name not outcome_type
  counts?: Record<string, number | undefined>;
  // kernel _run_view merges TaskRequest.context back in — the general
  // lane projects its TaskState/contract summary under context.general
  // (spec 005 §2.4; the exact sub-shape is still merging kernel-side —
  // consumers read defensively)
  context?: Record<string, unknown>;
  decision?: { answer: string; decided_by: string };
  error?: string;
}

export interface TaskResponse {
  status: string; // "clarify" | run status
  clarify_question?: string;
  router_via?: string;
  workrun_id?: string;
  // spec 005 AC1 — clarify payload carries false when no foundation
  // port is wired; when the general lane exists the kernel routes
  // unmatched intents to it directly (a clarify then means genuinely
  // ambiguous intent, not a dead end)
  assist_available?: boolean;
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

const API = "/api/ewcp";

function authHeaders(apiKey: string): Record<string, string> {
  return apiKey ? { "x-ewcp-api-key": apiKey } : {};
}

// Gateway CSRF middleware requires the double-submit pair on state-changing
// requests when auth is enabled — echo the csrf_token cookie back as a header.
function csrfHeader(): Record<string, string> {
  if (typeof document === "undefined") return {};
  const m = /(?:^|;\s*)csrf_token=([^;]+)/.exec(document.cookie);
  const token = m?.[1];
  return token ? { "x-csrf-token": decodeURIComponent(token) } : {};
}

function mutatingHeaders(apiKey: string): Record<string, string> {
  return { ...authHeaders(apiKey), ...csrfHeader() };
}

export async function createTask(
  args: {
    intent: string;
    tenant: string;
    apiKey: string;
    // spec-declared multipart inputs, keyed by requires_inputs[].name
    // from GET /outcomes (e.g. invoices_zip, books, dossier_zip)
    files?: Record<string, File | null>;
    // spec-declared context keys posted as plain form fields
    // (e.g. ky, mst_doanh_nghiep for invoice_recon)
    context?: Record<string, string>;
    // kernel honors Idempotency-Key (tenant-scoped) — retries of the same
    // logical submission reuse the caller's key instead of spawning a second run
    idempotencyKey?: string;
  },
): Promise<TaskResponse & Partial<RunView>> {
  const fd = new FormData();
  fd.set("intent", args.intent);
  fd.set("tenant_id", args.tenant);
  for (const [k, v] of Object.entries(args.context ?? {})) {
    if (v) fd.set(k, v);
  }
  for (const [name, f] of Object.entries(args.files ?? {})) {
    if (f) fd.set(name, f);
  }
  const res = await fetch(`${API}/tasks`, {
    method: "POST",
    headers: {
      ...mutatingHeaders(args.apiKey),
      ...(args.idempotencyKey
        ? { "Idempotency-Key": args.idempotencyKey }
        : {}),
    },
    body: fd,
  });
  return parse(res);
}

export async function supplyInputs(
  workrunId: string,
  args: {
    tenant: string;
    apiKey: string;
    files?: Record<string, File | null>;
  },
): Promise<RunView> {
  const fd = new FormData();
  fd.set("tenant_id", args.tenant);
  for (const [name, f] of Object.entries(args.files ?? {})) {
    if (f) fd.set(name, f);
  }
  const res = await fetch(`${API}/tasks/${workrunId}/inputs`, {
    method: "POST",
    headers: mutatingHeaders(args.apiKey),
    body: fd,
  });
  return parse(res);
}

// GET /outcomes — the pack registry as wire data (P0 pack contract).
// The pane renders slots/hints from this instead of hardcoding packs.
export interface OutcomeInputSpec {
  name: string; // multipart field name
  accept: string; // file-picker accept filter, e.g. ".zip" or ".csv,.xlsx"
  label_vn: string; // Vietnamese slot label
  required: boolean; // kernel InputReq.required — optional slots never block
}

export interface OutcomeSpecView {
  outcome_type: string;
  description: string;
  required_checks: string[];
  requires_inputs: OutcomeInputSpec[];
}

export async function listOutcomes(args: {
  apiKey: string;
}): Promise<OutcomeSpecView[]> {
  const res = await fetch(`${API}/outcomes`, {
    headers: authHeaders(args.apiKey),
  });
  return parse(res);
}

export async function getRun(
  workrunId: string,
  args: { apiKey: string },
): Promise<RunView> {
  const res = await fetch(`${API}/workruns/${workrunId}`, {
    headers: authHeaders(args.apiKey),
  });
  return parse(res);
}

export async function decide(
  workrunId: string,
  args: { apiKey: string; tenant: string; answer: string; decidedBy: string; decisionId?: string },
): Promise<RunView> {
  const res = await fetch(`${API}/workruns/${workrunId}/decisions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...mutatingHeaders(args.apiKey) },
    body: JSON.stringify({
      tenant_id: args.tenant,
      decided_by: args.decidedBy,
      answer: args.answer,
      decision_id: args.decisionId ?? null,
    }),
  });
  return parse(res);
}

export async function getManifest(
  workrunId: string,
  args: { apiKey: string },
): Promise<Record<string, unknown>> {
  const res = await fetch(`${API}/workruns/${workrunId}/manifest`, {
    headers: authHeaders(args.apiKey),
  });
  return parse(res);
}

export async function getEvidence(
  workrunId: string,
  args: { apiKey: string },
): Promise<Record<string, unknown>> {
  const res = await fetch(`${API}/workruns/${workrunId}/evidence`, {
    headers: authHeaders(args.apiKey),
  });
  return parse(res);
}

export async function verifyArtifacts(
  args: { evidenceJson: File; files: File[] },
): Promise<VerifyResult> {
  const fd = new FormData();
  fd.set("evidence_json", args.evidenceJson);
  for (const f of args.files) fd.append("files", f);
  const res = await fetch(`${API}/verify`, {
    method: "POST",
    headers: csrfHeader(),
    body: fd,
  });
  return parse(res);
}

// GET /verify/{manifest_hash} — public seal permalink. The kernel answers
// with no auth at all: the hash IS the capability (kernel api/app.py —
// "Public and cross-tenant by design"). Scope: proves a sealed manifest
// EXISTS and its HMAC seal recomputes; it does NOT re-measure artifact
// bytes — byte-integrity still needs the POST /verify upload path.
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
  // true = seal recomputes | false = seal mismatch | null = kernel has no
  // seal key configured (authenticity unprovable, manifest still resolves)
  seal_ok: boolean | null;
  manifest: {
    workrun_id?: string;
    manifest_hash?: string;
    seal?: string | null;
    deliverables?: PermalinkDeliverable[];
    checks?: PermalinkCheck[];
  };
}

export async function verifyPermalink(
  manifestHash: string,
): Promise<VerifyPermalinkResult> {
  const res = await fetch(
    `${API}/verify/${encodeURIComponent(manifestHash)}`,
  );
  return parse(res);
}

// The shareable URL a third party opens — routes back into this pane's
// public verify page with the hash in the query string.
export function verifyShareUrl(manifestHash: string): string {
  const origin =
    typeof window !== "undefined" ? window.location.origin : "";
  return `${origin}/ewcp/verify?manifest=${encodeURIComponent(manifestHash)}`;
}

export async function downloadDeliverable(
  workrunId: string,
  deliverableId: string,
  args: { apiKey: string; name?: string },
): Promise<void> {
  const res = await fetch(
    `${API}/workruns/${workrunId}/deliverables/${deliverableId}`,
    { headers: authHeaders(args.apiKey) },
  );
  if (!res.ok) throw new EwcpError(res.status, await res.text());
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = args.name ?? deliverableId;
  a.click();
  URL.revokeObjectURL(url);
}

export async function getOutcome(
  workrunId: string,
  args: { apiKey: string },
): Promise<{ outcome_type: string; result: unknown }> {
  const res = await fetch(`${API}/workruns/${workrunId}/outcome`, {
    headers: authHeaders(args.apiKey),
  });
  return parse(res);
}

// Bundled sample data — kernel GET /demo/fixtures/{name} (public, no auth).
export type DemoFixtureName =
  | "invoices"
  | "invoices_corrupt"
  | "books"
  | "books_corrupt"
  | "dossier"
  | "dossier_sea"
  | "three_way_po"
  | "three_way_receipts"
  | "three_way_invoices"
  | "three_way_po_clean"
  | "three_way_receipts_clean"
  | "three_way_invoices_clean"
  | "three_way_po_corrupt";

export async function fetchDemoFixture(
  name: DemoFixtureName,
): Promise<File> {
  const res = await fetch(`${API}/demo/fixtures/${name}`);
  if (!res.ok) throw new EwcpError(res.status, await res.text());
  const blob = await res.blob();
  const ext = res.headers
    .get("content-disposition")
    ?.match(/filename="?([^";]+)"?/)?.[1];
  return new File([blob], ext ?? `${name}.dat`, {
    type: blob.type || "application/octet-stream",
  });
}

export async function listRuns(
  args: { apiKey: string; tenant: string },
): Promise<RunView[]> {
  const res = await fetch(`${API}/workruns?tenant_id=${encodeURIComponent(args.tenant)}`, {
    headers: authHeaders(args.apiKey),
  });
  const body = await parse<{ items?: RunView[] } | RunView[]>(res);
  return Array.isArray(body) ? body : (body.items ?? []);
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

export class EwcpError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}
