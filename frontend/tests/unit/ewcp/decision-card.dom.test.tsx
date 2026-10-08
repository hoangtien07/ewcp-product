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

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

function stubFetch() {
  const calls: { url: string; init?: RequestInit }[] = [];
  rs.stubGlobal(
    "fetch",
    rs.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return Promise.resolve(
        new Response(JSON.stringify({ workrun_id: "wr1" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }),
  );
  return calls;
}

describe("DecisionCard", () => {
  test("approval options render Vietnamese labels", () => {
    render(
      <DecisionCard
        q={{
          decision_id: "",
          kind: "approval",
          prompt: "Duyệt kết quả",
          options: ["approve", "reject", "request_changes"],
        }}
        executionRunId="er-1"
        run={run}
        canDecide={true}
        onDone={rs.fn()}
      />,
    );
    expect(screen.getByText("Duyệt & niêm phong")).toBeTruthy();
    expect(screen.getByText("Từ chối")).toBeTruthy();
  });

  test("options_v2 posts the stable option id", async () => {
    const calls = stubFetch();
    render(
      <DecisionCard
        q={{
          decision_id: "d-9",
          kind: "option_choice",
          prompt: "Chọn?",
          options: [],
          options_v2: [
            { id: "keep_value", label: "Giữ nguyên" },
            { id: "fix_value", label: "Sửa lại" },
          ],
        }}
        executionRunId="er-1"
        run={run}
        canDecide={true}
        onDone={rs.fn()}
      />,
    );
    fireEvent.click(screen.getByText("Giữ nguyên"));
    await waitFor(() => expect(calls.length).toBe(1));
    expect(calls[0]!.url).toBe("/api/ewcp/runs/er-1/decisions");
    expect(JSON.parse(calls[0]!.init!.body as string)).toEqual({
      answer: "keep_value",
      decision_id: "d-9",
    });
  });

  test("canDecide=false renders read-only — buttons inert, no POST", () => {
    stubFetch();
    render(
      <DecisionCard
        q={{
          decision_id: "",
          kind: "approval",
          prompt: "Duyệt?",
          options: ["approve", "reject"],
        }}
        executionRunId="er-1"
        run={run}
        canDecide={false}
        onDone={rs.fn()}
      />,
    );
    expect(screen.getByText(/Chỉ xem/)).toBeTruthy();
    const btn = screen.getByText("Duyệt & niêm phong");
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });
});
