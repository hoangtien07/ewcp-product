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
} from "@/ewcp/labels";

describe("ewcp labels — kernel enum → Vietnamese", () => {
  test("approval answers map to the kernel static-UI vocabulary", () => {
    // mirrors kernel src/ewcp/api/static/index.html approval buttons
    expect(DECISION_LABEL.approve).toBe("Duyệt & niêm phong");
    expect(DECISION_LABEL.reject).toBe("Từ chối");
    expect(DECISION_LABEL.request_changes).toBe("Yêu cầu sửa");
    expect(decisionLabel("approve")).toBe("Duyệt & niêm phong");
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

  test("formatCounts renders pack summarize() keys generically", () => {
    // keys come from the pack's spec.summarize — formatting must not
    // switch on outcome_type
    expect(formatCounts({ invoices: 3, book_rows: 12 })).toBe(
      "3 hóa đơn · 12 dòng sổ",
    );
    expect(formatCounts({ docs: 7 })).toBe("7 chứng từ");
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
  });
});
