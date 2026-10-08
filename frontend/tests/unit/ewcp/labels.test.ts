import { describe, expect, test } from "@rstest/core";

import {
  DECISION_LABEL,
  decisionLabel,
  formatCounts,
  outcomeLabel,
  RECON_STATUS_LABEL,
  reconStatusLabel,
  STATUS_LABEL,
  statusLabel,
  THREE_WAY_FLAG_LABEL,
  THREE_WAY_STATUS_LABEL,
  threeWayFlagLabel,
  threeWayStatusLabel,
} from "@/ewcp/labels";

describe("ewcp labels — kernel enum → Vietnamese", () => {
  test("approval answers map to the kernel static-UI vocabulary", () => {
    // mirrors kernel src/ewcp/api/static/index.html approval buttons
    expect(DECISION_LABEL.approve).toBe("Duyệt & niêm phong");
    expect(DECISION_LABEL.reject).toBe("Từ chối");
    expect(DECISION_LABEL.request_changes).toBe("Yêu cầu sửa");
    expect(decisionLabel("approve")).toBe("Duyệt & niêm phong");
  });

  test("contract-approval answers map to spec-005 vocabulary", () => {
    // spec 005 §7.3 — contract_approval options (approve_contract /
    // revise_contract) arrive as bare ids on the legacy `options` path
    expect(DECISION_LABEL.approve_contract).toBe("Duyệt tiêu chí & chạy tiếp");
    expect(DECISION_LABEL.revise_contract).toBe("Yêu cầu sửa tiêu chí");
    expect(decisionLabel("approve_contract")).toBe(
      "Duyệt tiêu chí & chạy tiếp",
    );
  });

  test("every kernel TaskStatus has a Vietnamese label", () => {
    // kernel TaskStatus enum (src/ewcp/kernel/models.py)
    const kernelStatuses = [
      "received",
      "running",
      "awaiting_approval",
      "awaiting_input",
      "candidate_complete",
      "verified",
      "failed",
      "cancelled",
    ];
    for (const s of kernelStatuses) {
      expect(STATUS_LABEL[s], `missing label for ${s}`).toBeTruthy();
      expect(statusLabel(s)).not.toBe(s);
    }
    expect(statusLabel("candidate_complete")).toBe("Chờ duyệt");
    expect(statusLabel("awaiting_input")).toBe("Chờ thêm đầu vào");
    expect(statusLabel("verified")).toBe("Đã niêm phong");
  });

  test("outcome types map to Vietnamese surface names", () => {
    expect(outcomeLabel("invoice_recon")).toBe("Đối soát hóa đơn");
    expect(outcomeLabel("dossier_check")).toBe("Kiểm tra chứng từ");
    expect(outcomeLabel("three_way_match")).toBe("Đối chiếu 3 chiều");
    expect(outcomeLabel("general")).toBe("Lane tổng quát");
  });

  test("every kernel ReconStatus has a Vietnamese chip label", () => {
    // kernel ReconStatus enum (invoice_recon/recon/match.py)
    const kernelReconStatuses = [
      "MATCHED",
      "MISSING_BOOK",
      "MISSING_INVOICE",
      "DUPLICATE",
      "AMOUNT_MISMATCH",
      "NEEDS_REVIEW",
      "UNVERIFIED_SOURCE",
    ];
    for (const s of kernelReconStatuses) {
      expect(RECON_STATUS_LABEL[s], `missing label for ${s}`).toBeTruthy();
    }
    expect(reconStatusLabel("AMOUNT_MISMATCH")).toBe("Lệch số tiền");
  });

  test("every kernel MatchStatus has a Vietnamese chip label", () => {
    // kernel MatchStatus enum (three_way_match/match/match.py)
    const kernelMatchStatuses = [
      "MATCHED",
      "PARTIAL_RECEIPT",
      "OVER_RECEIPT",
      "OVER_BILLED",
      "PRICE_VARIANCE",
      "AMOUNT_VARIANCE",
      "TERMS_MISMATCH",
      "NO_PO",
      "NO_RECEIPT",
      "CURRENCY_MISMATCH",
      "DUPLICATE",
      "DORMANT_PO",
      "NEEDS_REVIEW",
    ];
    for (const s of kernelMatchStatuses) {
      expect(THREE_WAY_STATUS_LABEL[s], `missing label for ${s}`).toBeTruthy();
    }
    expect(threeWayStatusLabel("PRICE_VARIANCE")).toBe("Lệch đơn giá");
    expect(threeWayStatusLabel("NO_PO")).toBe("Không có PO");
  });

  test("every kernel three-way wire flag has a Vietnamese chip label", () => {
    // kernel eval_status/extra_flags tokens (three_way_match match+spec)
    const kernelWireFlags = [
      "currency_mismatch",
      "no_receipt",
      "partial_receipt",
      "over_receipt",
      "over_billed",
      "price_variance",
      "amount_variance",
      "terms_mismatch",
      "duplicate",
      "no_po",
      "unmatched_receipt_line",
      "unmatched_invoice_line",
      "dormant",
      "buyer_mst_mismatch",
      "ky_out_of_range",
    ];
    for (const f of kernelWireFlags) {
      expect(THREE_WAY_FLAG_LABEL[f], `missing label for ${f}`).toBeTruthy();
    }
    expect(threeWayFlagLabel("price_variance")).toBe("lệch đơn giá");
  });

  test("formatCounts renders pack summarize() keys generically", () => {
    // keys come from the pack's spec.summarize — formatting must not
    // switch on outcome_type
    expect(formatCounts({ invoices: 3, book_rows: 12 })).toBe(
      "3 hóa đơn · 12 dòng sổ",
    );
    expect(formatCounts({ docs: 7 })).toBe("7 chứng từ");
    // three_way_match emits n_pos/n_receipts/n_invoices verbatim
    expect(formatCounts({ n_pos: 11, n_receipts: 9, n_invoices: 12 })).toBe(
      "11 PO · 9 phiếu nhận · 12 hóa đơn",
    );
    expect(formatCounts({ future_key: 2 })).toBe("2 future_key");
    expect(formatCounts({})).toBe("");
  });

  test("unknown values fall through untouched (kernel VN options pass through)", () => {
    // kernel ask-back options already arrive as Vietnamese prose — the map
    // must not mangle them
    expect(decisionLabel("giữ nguyên và trình duyệt")).toBe(
      "giữ nguyên và trình duyệt",
    );
    expect(statusLabel("some_future_status")).toBe("some_future_status");
    expect(outcomeLabel("other_outcome")).toBe("other_outcome");
    expect(reconStatusLabel("OTHER")).toBe("OTHER");
    expect(threeWayStatusLabel("OTHER")).toBe("OTHER");
    expect(threeWayFlagLabel("future_flag")).toBe("future_flag");
  });
});
