import { afterEach, describe, expect, rs, test } from "@rstest/core";
import { cleanup, render, screen } from "@testing-library/react";

import type { RunView } from "@/ewcp/api";
import { DecisionCard } from "@/ewcp/components/decision-card";

const run = {
  workrun_id: "wr1",
  tenant_id: "demo",
  outcome_type: "invoice_recon",
  status: "candidate_complete",
  step_label: "",
  intent: "",
  pending_questions: [],
  deliverables: [],
  ingest_errors: [],
  skipped: [],
} as RunView;

const creds = { apiKey: "", tenant: "demo", decidedBy: "boss" };

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

describe("DecisionCard", () => {
  test("approval options render Vietnamese labels, not raw enum strings", () => {
    render(
      <DecisionCard
        q={{
          decision_id: "",
          kind: "approval",
          prompt: "Duyệt kết quả",
          options: ["approve", "reject", "request_changes"],
        }}
        run={run}
        creds={creds}
        onDone={rs.fn()}
      />,
    );
    expect(screen.getByText("Duyệt & niêm phong")).toBeTruthy();
    expect(screen.getByText("Từ chối")).toBeTruthy();
    expect(screen.getByText("Yêu cầu sửa")).toBeTruthy();
    expect(screen.queryByText("request_changes")).toBeNull();
  });

  test("kernel-provided Vietnamese option strings pass through unmapped", () => {
    render(
      <DecisionCard
        q={{
          decision_id: "d1",
          kind: "option_choice",
          prompt: "Xử lý thế nào?",
          options: [
            "giữ nguyên và trình duyệt",
            "bỏ qua dòng lệch và chạy lại",
          ],
        }}
        run={run}
        creds={creds}
        onDone={rs.fn()}
      />,
    );
    expect(screen.getByText("giữ nguyên và trình duyệt")).toBeTruthy();
  });

  test("disabledHint keeps buttons inert and shows the hint (409 guard)", () => {
    const fetchSpy = rs.fn();
    rs.stubGlobal("fetch", fetchSpy);
    render(
      <DecisionCard
        q={{
          decision_id: "",
          kind: "approval",
          prompt: "Duyệt kết quả",
          options: ["approve", "reject", "request_changes"],
        }}
        run={run}
        creds={creds}
        onDone={rs.fn()}
        disabledHint="Trả lời hết câu hỏi trước"
      />,
    );
    expect(screen.getByText("Trả lời hết câu hỏi trước")).toBeTruthy();
    const approveBtn = screen
      .getByText("Duyệt & niêm phong")
      .closest("button")!;
    expect(approveBtn.disabled).toBe(true);
    approveBtn.click();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
