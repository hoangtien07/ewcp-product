import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import type { RunView } from "@/ewcp/api";
import { StudioCard } from "@/ewcp/components/studio-card";

const run = {
  workrun_id: "wr-tw",
  tenant_id: "demo",
  outcome_type: "three_way_match",
  status: "candidate_complete",
  step_label: "",
  intent: "",
  pending_questions: [],
  deliverables: [
    {
      deliverable_id: "d1",
      kind: "file",
      sha256: "x",
      uri: "a",
      name: "three_way_result.json",
    },
  ],
  ingest_errors: [],
  skipped: [],
} as RunView;

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

// kernel three_way_match.export_json → three_way_result.json
const result = {
  verdict: "fail",
  params: { buyer_mst: "0300000001", ky: "2025-09" },
  rows: [
    {
      status: "MATCHED",
      severity: "info",
      source: "po:PO-001:L1",
      sources: [],
      line_refs: [],
      flags: [],
      po_no: "PO-001",
      label: "SKU-A",
      ordered_qty: "10",
      received_qty: "10",
      invoiced_qty: "10",
      ordered_price: "1000",
      invoiced_amount: "10000",
      currency: "VND",
      detail: "",
    },
    {
      status: "PARTIAL_RECEIPT",
      severity: "warn",
      source: "po:PO-002:L1",
      sources: ["rcpt:receipts_main.csv:doc"],
      line_refs: [],
      flags: ["partial_receipt", "no_receipt"],
      po_no: "PO-002",
      label: "SKU-B",
      ordered_qty: "20",
      received_qty: "12",
      invoiced_qty: "12",
      ordered_price: "500",
      invoiced_amount: "6000",
      currency: "VND",
      detail: "nhận thiếu 8",
    },
    {
      status: "NO_PO",
      severity: "fail",
      source: "inv:inv_999.xml:L1",
      sources: [],
      line_refs: [],
      flags: ["no_po"],
      po_no: "",
      label: "",
      ordered_qty: null,
      received_qty: null,
      invoiced_qty: "3",
      ordered_price: null,
      invoiced_amount: "3000",
      currency: "VND",
      detail: "hóa đơn không có PO",
    },
  ],
  inputs: { documents: [] },
};

function stubOutcome(body: unknown = result) {
  rs.stubGlobal(
    "fetch",
    rs.fn(async () => {
      return new Response(
        JSON.stringify({ outcome_type: "three_way_match", result: body }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }),
  );
}

describe("StudioCard — three_way_match", () => {
  test("renders PO-line exceptions with qty triple + flag chips", async () => {
    stubOutcome();
    render(<StudioCard run={run} apiKey="" />);
    await waitFor(() =>
      expect(screen.getByText("Đối chiếu 3 chiều")).toBeTruthy(),
    );
    // header counters: 1 of 3 rows matched, 2 exceptions
    expect(screen.getByText(/1\/3 dòng khớp/)).toBeTruthy();
    expect(screen.getByText("Có lỗi — cần xử lý")).toBeTruthy();
    // run context (ky + buyer MST) surfaces from params
    expect(screen.getByText(/Kỳ 2025-09/)).toBeTruthy();
    // PO-line row: status chip in Vietnamese, po_no, qty triple, flags
    expect(screen.getByText("Nhận một phần")).toBeTruthy();
    expect(screen.getByText("PO-002")).toBeTruthy();
    expect(screen.getByText(/đặt 20 · nhận 12 · HĐ 12/)).toBeTruthy();
    expect(screen.getByText("nhận một phần")).toBeTruthy();
    expect(screen.getByText("chưa nhận")).toBeTruthy();
    expect(screen.getByText("nhận thiếu 8")).toBeTruthy();
    // matched row stays out of the exception list
    expect(screen.queryByText("PO-001")).toBeNull();
    // doc-level row lands in the Chứng từ section
    expect(screen.getByText("Chứng từ")).toBeTruthy();
    expect(screen.getByText("Không có PO")).toBeTruthy();
    expect(screen.getByText("inv:inv_999.xml:L1")).toBeTruthy();
  });

  test("all-matched payload renders the all-clear state", async () => {
    stubOutcome({
      verdict: "pass",
      params: { buyer_mst: "0300000001", ky: "2025-09" },
      rows: [result.rows[0]],
      inputs: { documents: [] },
    });
    render(<StudioCard run={run} apiKey="" />);
    await waitFor(() =>
      expect(screen.getByText("Mọi dòng khớp đủ 3 chiều — không có ngoại lệ."))
        .toBeTruthy(),
    );
    expect(screen.getByText("Đạt")).toBeTruthy();
  });

  test("malformed payload renders nothing (no crash on missing rows)", async () => {
    stubOutcome({ verdict: "pass" });
    const { container } = render(<StudioCard run={run} apiKey="" />);
    await waitFor(() => expect(container.children).toHaveLength(0));
  });
});
