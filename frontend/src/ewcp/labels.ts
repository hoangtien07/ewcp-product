// EWCP label maps — kernel wire values are English enums; every user-facing
// surface renders Vietnamese. Vocabulary mirrors the kernel's own static UI
// (kernel repo src/ewcp/api/static/index.html) so the pane and the kernel
// demo tell the user the same words.

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
};

// RunView.outcome_type → Vietnamese surface name (history rail).
export const OUTCOME_LABEL: Record<string, string> = {
  invoice_recon: "Đối soát hóa đơn",
  dossier_check: "Kiểm tra chứng từ",
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

export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}

export function decisionLabel(answer: string): string {
  return DECISION_LABEL[answer] ?? answer;
}

export function outcomeLabel(outcomeType: string): string {
  return OUTCOME_LABEL[outcomeType] ?? outcomeType;
}

export function reconStatusLabel(status: string): string {
  return RECON_STATUS_LABEL[status] ?? status;
}
