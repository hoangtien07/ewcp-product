import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

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

function fetchCalls() {
  const calls: { url: string; init?: RequestInit }[] = [];
  rs.stubGlobal(
    "fetch",
    rs.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ workrun_id: "wr1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

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

  test("options_v2 posts the option id while the label renders", async () => {
    const calls = fetchCalls();
    render(
      <DecisionCard
        q={{
          decision_id: "d1",
          kind: "option_choice",
          prompt: "Dòng lệch — xử lý?",
          options: ["giữ nguyên và trình duyệt"],
          options_v2: [
            { id: "keep_and_submit", label: "giữ nguyên và trình duyệt" },
          ],
        }}
        run={run}
        creds={creds}
        onDone={rs.fn()}
      />,
    );
    fireEvent.click(screen.getByText("giữ nguyên và trình duyệt"));
    await waitFor(() => expect(calls).toHaveLength(1));
    const body = JSON.parse(calls.at(0)?.init?.body as string);
    expect(body.answer).toBe("keep_and_submit");
    expect(body.decision_id).toBe("d1");
  });

  test("without options_v2 the label posts verbatim (legacy kernel)", async () => {
    const calls = fetchCalls();
    render(
      <DecisionCard
        q={{
          decision_id: "d2",
          kind: "option_choice",
          prompt: "Dòng lệch — xử lý?",
          options: ["giữ nguyên và trình duyệt"],
        }}
        run={run}
        creds={creds}
        onDone={rs.fn()}
      />,
    );
    fireEvent.click(screen.getByText("giữ nguyên và trình duyệt"));
    await waitFor(() => expect(calls).toHaveLength(1));
    const body = JSON.parse(calls.at(0)?.init?.body as string);
    expect(body.answer).toBe("giữ nguyên và trình duyệt");
  });

  test("contract_approval kind renders and posts approve_contract id", async () => {
    const calls = fetchCalls();
    render(
      <DecisionCard
        q={{
          decision_id: "d-c1",
          kind: "contract_approval",
          prompt:
            "Agent đề xuất tiêu chí nghiệm thu:\n- c1: file tồn tại",
          options: ["approve_contract", "revise_contract"],
          options_v2: [
            { id: "approve_contract", label: "Duyệt tiêu chí & chạy tiếp" },
            { id: "revise_contract", label: "Yêu cầu sửa tiêu chí" },
          ],
        }}
        run={run}
        creds={creds}
        onDone={rs.fn()}
      />,
    );
    expect(screen.getByText("Duyệt tiêu chí nghiệm thu")).toBeTruthy();
    const approveBtn = screen
      .getByText("Duyệt tiêu chí & chạy tiếp")
      .closest("button")!;
    // approve-family styling (emerald), not the neutral zinc button
    expect(approveBtn.className).toContain("emerald");
    fireEvent.click(approveBtn);
    await waitFor(() => expect(calls).toHaveLength(1));
    const body = JSON.parse(calls.at(0)?.init?.body as string);
    expect(body.answer).toBe("approve_contract");
    expect(body.decision_id).toBe("d-c1");
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
