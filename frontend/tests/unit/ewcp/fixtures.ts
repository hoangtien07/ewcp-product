// Canned EWCP wire fixtures — mirrors the kernel contract pinned in
// kernel repo `tests/test_api_contract.py` (lowercase TaskStatus values,
// options_v2 slugs, tri-state seal_ok) and the extension's record view
// (`ewcp_core/api_routes.py::_record_view`). Used by the A6 docs-aid
// contract test and reusable by future pane tests.

import type { ExecutionRun, RunView } from "@/ewcp/api";

/** A governed ExecutionRun bound to a verified kernel workrun — the
 * state a run shows after the approve + seal round-trip. */
export const GOVERNED_EXECUTION_RUN: ExecutionRun = {
  execution_run_id: "er_fixture1",
  thread_id: "thread_fixture1",
  run_id: "run_fixture1",
  workrun_id: "wr_fixture1",
  task_mode: "governed",
  status: "completed",
  intent: "Đối soát hóa đơn đầu vào kỳ 09/2025",
  idempotency_key: "ewcp.core:fixture",
  created_by: "u-fixture",
  created_at: "2026-10-09T00:00:00Z",
  updated_at: "2026-10-09T00:05:00Z",
  join_url: null,
};

/** Kernel RunView at `verified` — post-approval, manifest sealed.
 * `options_v2` on pending_questions is empty here because a verified run
 * has no open decisions; see PENDING_QUESTION for a live ask-back. */
export const VERIFIED_RUN_VIEW: RunView = {
  workrun_id: "wr_fixture1",
  tenant_id: "demo",
  outcome_type: "invoice_recon",
  status: "verified",
  step_label: "Hoàn tất",
  intent: "Đối soát hóa đơn đầu vào kỳ 09/2025",
  pending_questions: [],
  deliverables: [
    {
      deliverable_id: "d-report",
      kind: "result",
      sha256: "a".repeat(64),
      uri: "ewcp://deliverables/d-report",
      name: "ket_qua_doi_soat.xlsx",
    },
    {
      deliverable_id: "d-evidence",
      kind: "contract",
      sha256: "b".repeat(64),
      uri: "ewcp://deliverables/d-evidence",
      name: "evidence.json",
    },
  ],
  ingest_errors: [],
  skipped: [],
  counts: { invoices: 12, book_rows: 12 },
  decision: { answer: "approve", decided_by: "user:u-fixture" },
};

/** A live `option_choice` pending question in the options_v2 slug
 * contract — pane posts `.id`, kernel dispatches on it. */
export const PENDING_QUESTION = {
  decision_id: "dec_fixture1",
  kind: "option_choice",
  prompt: "Hóa đơn #7 lệch 5,000đ so với sổ — xử lý thế nào?",
  options: [],
  options_v2: [
    {
      id: "accept_variance",
      label: "Chấp nhận chênh lệch, ghi chú vào kết quả",
    },
    { id: "hold_invoice", label: "Giữ hóa đơn chờ đối chiếu lại" },
  ],
};

/** The manifest payload `GET /runs/{id}/manifest` returns for the
 * verified run — ManifestCard renders only after this resolves. */
export const SEALED_MANIFEST = {
  workrun_id: "wr_fixture1",
  manifest_hash: "c".repeat(64),
  seal: "d".repeat(64),
  checks: [
    { name: "input_partition", result: "PASS", detail: "12/12 phân loại được" },
    { name: "totals_match", result: "PASS", detail: "Tổng sổ = tổng hóa đơn" },
    { name: "traceability", result: "PASS", detail: "" },
  ],
  deliverables: VERIFIED_RUN_VIEW.deliverables.map(
    ({ deliverable_id, kind, sha256, name }) => ({
      deliverable_id,
      kind,
      sha256,
      name,
    }),
  ),
};
