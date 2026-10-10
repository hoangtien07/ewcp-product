// EWCP label maps — kernel wire values are English enums; every user-facing
// surface renders Vietnamese. Vocabulary mirrors the kernel's own static UI
// (kernel repo src/ewcp/api/static/index.html) so the pane and the kernel
// demo tell the user the same words.

import type { ExecutionRun, RunView } from "./api";

// kernel TaskStatus values (lowercase enums) → Vietnamese pill text.
// "rejected" is not a kernel TaskStatus but is kept for the pane's
// defensive red styling of a rejected run.
export const STATUS_LABEL: Record<string, string> = {
  received: "Đã nhận",
  running: "Đang xử lý",
  awaiting_approval: "Chờ phê duyệt",
  awaiting_input: "Chờ thêm đầu vào",
  candidate_complete: "Chờ duyệt",
  verified: "Đã niêm phong",
  failed: "Lỗi",
  cancelled: "Đã hủy",
  rejected: "Đã từ chối",
};

// Approval-gate answers — the only English option values the kernel emits
// (ask-back question options already arrive as Vietnamese prose).
export const DECISION_LABEL: Record<string, string> = {
  approve: "Duyệt & niêm phong",
  reject: "Từ chối",
  request_changes: "Yêu cầu sửa",
  // spec 005 §7.3 contract_approval options — bare ids on the legacy
  // `options` path (options_v2 already ships VN labels verbatim)
  approve_contract: "Duyệt tiêu chí & chạy tiếp",
  revise_contract: "Yêu cầu sửa tiêu chí",
};

// RunView.outcome_type → Vietnamese surface name (history rail, gallery).
// bank_recon is the announced roadmap pack — it renders as a "Sắp có"
// card until the kernel registers it.
export const OUTCOME_LABEL: Record<string, string> = {
  invoice_recon: "Đối soát hóa đơn",
  dossier_check: "Kiểm tra chứng từ",
  three_way_match: "Đối chiếu 3 chiều",
  bank_recon: "Đối chiếu sao kê ngân hàng",
  general: "Công việc tổng quát",
};

// Deliverable.kind → Vietnamese chip in the workspace deliverables list.
// contract = the kernel-written acceptance-contract copy harvested as a
// deliverable (spec 005 §8); unknown kinds pass through verbatim.
export const DELIVERABLE_KIND_LABEL: Record<string, string> = {
  contract: "hợp đồng nghiệm thu",
  result: "kết quả",
};

// RunView.counts keys → Vietnamese unit names. The wire carries whatever
// keys the pack's spec.summarize emits — the pane formats every numeric
// entry generically instead of switching on outcome_type. Legacy display
// names (invoices/book_rows/docs) and raw n_* keys are both covered so
// the label survives whichever rename map a kernel version ships.
export const COUNT_LABEL: Record<string, string> = {
  invoices: "hóa đơn",
  book_rows: "dòng sổ",
  docs: "chứng từ",
  n_invoices: "hóa đơn",
  pos: "PO",
  receipts: "phiếu nhận",
  n_pos: "PO",
  n_receipts: "phiếu nhận",
};

// invoice_recon row statuses (kernel ReconStatus enum) shown as chips in
// the exception grid. Kernel ships no grid vocabulary — mapped here.
export const RECON_STATUS_LABEL: Record<string, string> = {
  MATCHED: "Khớp",
  MISSING_BOOK: "Thiếu dòng sổ",
  MISSING_INVOICE: "Thiếu hóa đơn",
  DUPLICATE: "Trùng",
  AMOUNT_MISMATCH: "Lệch số tiền",
  NEEDS_REVIEW: "Cần xem xét",
  UNVERIFIED_SOURCE: "Chưa xác minh nguồn",
};

// three_way_match row statuses (kernel MatchStatus enum,
// three_way_match/match/match.py) — studio card chips.
export const THREE_WAY_STATUS_LABEL: Record<string, string> = {
  MATCHED: "Khớp",
  PARTIAL_RECEIPT: "Nhận một phần",
  OVER_RECEIPT: "Nhận vượt",
  OVER_BILLED: "HĐ vượt số nhận",
  PRICE_VARIANCE: "Lệch đơn giá",
  AMOUNT_VARIANCE: "Lệch thành tiền",
  TERMS_MISMATCH: "HĐ trước nhận hàng",
  NO_PO: "Không có PO",
  NO_RECEIPT: "Chưa nhận hàng",
  CURRENCY_MISMATCH: "Lệch tiền tệ",
  DUPLICATE: "Trùng",
  DORMANT_PO: "PO chưa phát sinh",
  NEEDS_REVIEW: "Cần xem xét",
};

// three_way_match per-line flags (kernel eval_status/extra_flags) —
// snake_case wire tokens rendered as small chips under each PO line.
export const THREE_WAY_FLAG_LABEL: Record<string, string> = {
  currency_mismatch: "lệch tiền tệ",
  no_receipt: "chưa nhận",
  partial_receipt: "nhận một phần",
  over_receipt: "nhận vượt",
  over_billed: "HĐ vượt nhận",
  price_variance: "lệch đơn giá",
  amount_variance: "lệch thành tiền",
  terms_mismatch: "HĐ trước nhận",
  duplicate: "trùng",
  no_po: "không PO",
  unmatched_receipt_line: "dòng nhận lạc",
  unmatched_invoice_line: "dòng HĐ lạc",
  dormant: "chưa phát sinh",
  buyer_mst_mismatch: "lệch MST bên mua",
  ky_out_of_range: "ngoài kỳ",
};

export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}

// ExecutionRun.task_mode → Vietnamese lane name (run-list chip, thread
// header, chat badge). governed = the contract-bound lane whose outcome
// is a sealed manifest; invoke = a governed capability launched from a
// chat thread (A6 #13). "Kiểm chứng" is reserved for these governed
// verification surfaces — the free-form workspace stays "Công việc".
export const TASK_MODE_LABEL: Record<string, string> = {
  general: "tổng quát",
  governed: "kiểm chứng",
  invoke: "kiểm chứng từ chat",
};

export function taskModeLabel(mode: string): string {
  return TASK_MODE_LABEL[mode] ?? mode;
}

// Deliverable-integrity flag prefix — mirrors the extension's
// `FLAG_CLAIMED_MISSING_PREFIX` (ewcp_core/deliverable_integrity.py). The
// flag is the F2 delivery verdict on a general-lane run: it records the
// `/mnt/user-data/**` deliverables the agent claimed but the host probe
// could not find (or found empty). `verified`/`no_claims` carry no list.
export const MISSING_CLAIMS_PREFIX = "claimed_artifacts_missing:";

/** Decode `integrity_flag` → the claimed-but-missing artifact names.
 * Returns null when the flag does not carry the missing-claims prefix
 * (absent, `verified`, `no_claims`). The prefix itself is the verdict: a
 * present-but-unparseable payload still means claims were flagged —
 * returns `[]` so callers surface the generic unverified marker. */
export function missingClaimedArtifacts(
  flag: string | null | undefined,
): string[] | null {
  if (!flag?.startsWith(MISSING_CLAIMS_PREFIX)) return null;
  try {
    const parsed: unknown = JSON.parse(
      flag.slice(MISSING_CLAIMS_PREFIX.length),
    );
    return Array.isArray(parsed) ? (parsed as string[]) : [];
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------------------
// WP-A6 unified lifecycle vocabulary (kernel repo
// docs/program/MASTER_EXECUTION_PLAN.md §WP-A6). ONE status story per run,
// projected over the two raw enums — the launcher projection
// (ExecutionRunStatus) and kernel truth (TaskStatus on RunView). A bound
// workrun wins: the kernel owns governed truth. No kernel view → the
// launcher record alone. The write-path states (approved_to_act as a
// kernel status, external_executed) have no producer until A5b — the
// switch passes them through so a kernel that ships them is labeled
// without a pane change.
// ---------------------------------------------------------------------------

export type LifecycleStatus =
  | "accepted"
  | "agent_finished"
  | "verified"
  | "approved_to_act"
  | "external_executed"
  | "UNKNOWN";

/** One lifecycle answer for a run + its optional kernel view. Terminal
 * failures (failed/timeout/cancelled) and unrecognized values claim no
 * checkpoint — UNKNOWN, not a guess. `integrity_flag` is optional so
 * synthetic run shapes (invoke step) keep compiling. */
export function lifecycleStatus(
  run: Pick<ExecutionRun, "status"> & { integrity_flag?: string | null },
  workrun?: Pick<RunView, "status" | "decision"> | null,
): LifecycleStatus {
  if (workrun) {
    switch (workrun.status) {
      case "external_executed":
        return "external_executed";
      case "approved_to_act":
        return "approved_to_act";
      case "verified":
        return "verified";
      case "candidate_complete":
      case "awaiting_approval":
        // the agent's part is done — the ball is with verification or
        // the human gate
        return "agent_finished";
      case "received":
      case "running":
      case "awaiting_input":
      case "interrupted":
        // an approved AcceptanceContract upgrades mid-flight work to
        // approved_to_act (spec 005 §7.4 — the agent proceeds under the
        // bound criteria)
        return workrun.decision?.answer === "approve_contract"
          ? "approved_to_act"
          : "accepted";
      default:
        return "UNKNOWN";
    }
  }
  switch (run.status) {
    case "completed":
      // N01/F2: `completed` is lifecycle truth only, not delivery truth.
      // When the deliverable-integrity probe flagged missing claims, the
      // run's self-report is unverified — claiming agent_finished would
      // surface a false success, so the run claims no checkpoint (UNKNOWN).
      // no_claims / verified / unassessed (null) keep it: a legit
      // text-only answer is not a failure.
      return missingClaimedArtifacts(run.integrity_flag) !== null
        ? "UNKNOWN"
        : "agent_finished";
    case "launching":
    case "running":
    case "pending_interrupt":
    case "interrupted":
      return "accepted";
    default:
      return "UNKNOWN";
  }
}

export const LIFECYCLE_LABEL: Record<LifecycleStatus, string> = {
  accepted: "Đã tiếp nhận",
  agent_finished: "Agent hoàn tất",
  verified: "Đã niêm phong",
  approved_to_act: "Đã duyệt tác động",
  external_executed: "Đã tác động bên ngoài",
  UNKNOWN: "Không rõ",
};

export function lifecycleLabel(
  run: Pick<ExecutionRun, "status"> & { integrity_flag?: string | null },
  workrun?: Pick<RunView, "status" | "decision"> | null,
): string {
  return LIFECYCLE_LABEL[lifecycleStatus(run, workrun)];
}

export function decisionLabel(answer: string): string {
  return DECISION_LABEL[answer] ?? answer;
}

export function outcomeLabel(outcomeType: string): string {
  return OUTCOME_LABEL[outcomeType] ?? outcomeType;
}

export function deliverableKindLabel(kind: string): string {
  return DELIVERABLE_KIND_LABEL[kind] ?? kind;
}

export function reconStatusLabel(status: string): string {
  return RECON_STATUS_LABEL[status] ?? status;
}

export function threeWayStatusLabel(status: string): string {
  return THREE_WAY_STATUS_LABEL[status] ?? status;
}

export function threeWayFlagLabel(flag: string): string {
  return THREE_WAY_FLAG_LABEL[flag] ?? flag;
}

export function formatCounts(
  counts: Record<string, number | undefined>,
): string {
  return Object.entries(counts)
    .filter((kv): kv is [string, number] => typeof kv[1] === "number")
    .map(([k, v]) => `${v} ${COUNT_LABEL[k] ?? k}`)
    .join(" · ");
}
