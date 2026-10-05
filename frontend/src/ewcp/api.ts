// EWCP kernel API client — talks to /api/ewcp/* (Next rewrite → Gateway →
// extension router → in-process kernel mount). Auth: x-ewcp-api-key when the
// tenant entered a key, else tenant_id form field (dev/demo mode).

export interface PendingQuestion {
  decision_id: string;
  kind: string; // missing_input | option_choice | confirm_value | approval
  prompt: string;
  options: string[];
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
  counts?: { invoices: number; book_rows: number; docs?: number };
  decision?: { answer: string; decided_by: string };
  error?: string;
}

export interface TaskResponse {
  status: string; // "clarify" | run status
  clarify_question?: string;
  router_via?: string;
  workrun_id?: string;
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
    ky?: string;
    mst?: string;
    invoicesZip?: File | null;
    books?: File | null;
    dossierZip?: File | null;
  },
): Promise<TaskResponse & Partial<RunView>> {
  const fd = new FormData();
  fd.set("intent", args.intent);
  fd.set("tenant_id", args.tenant);
  if (args.ky) fd.set("ky", args.ky);
  if (args.mst) fd.set("mst_doanh_nghiep", args.mst);
  if (args.invoicesZip) fd.set("invoices_zip", args.invoicesZip);
  if (args.books) fd.set("books", args.books);
  if (args.dossierZip) fd.set("dossier_zip", args.dossierZip);
  const res = await fetch(`${API}/tasks`, {
    method: "POST",
    headers: mutatingHeaders(args.apiKey),
    body: fd,
  });
  return parse(res);
}

export async function supplyInputs(
  workrunId: string,
  args: { tenant: string; apiKey: string; invoicesZip?: File | null; books?: File | null; dossierZip?: File | null },
): Promise<RunView> {
  const fd = new FormData();
  fd.set("tenant_id", args.tenant);
  if (args.invoicesZip) fd.set("invoices_zip", args.invoicesZip);
  if (args.books) fd.set("books", args.books);
  if (args.dossierZip) fd.set("dossier_zip", args.dossierZip);
  const res = await fetch(`${API}/tasks/${workrunId}/inputs`, {
    method: "POST",
    headers: mutatingHeaders(args.apiKey),
    body: fd,
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
