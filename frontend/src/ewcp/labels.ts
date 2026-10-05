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
  three_way_match: "Đối chiếu 3 chiều",
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

export function decisionLabel(answer: string): string {
  return DECISION_LABEL[answer] ?? answer;
}

export function outcomeLabel(outcomeType: string): string {
  return OUTCOME_LABEL[outcomeType] ?? outcomeType;
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
