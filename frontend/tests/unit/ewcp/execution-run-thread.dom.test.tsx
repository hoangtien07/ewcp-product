import { afterEach, describe, expect, rs, test } from "@rstest/core";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

import { ExecutionRunThread } from "@/ewcp/components/execution-run-thread";

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

const RUN = {
  execution_run_id: "er-1",
  thread_id: "t-1",
  run_id: "r-1",
  workrun_id: "wr-1",
  task_mode: "governed",
  status: "completed",
  intent: "đối soát",
  idempotency_key: "k",
  created_by: "u-1",
  created_at: "x",
  updated_at: "x",
  join_url: "/api/threads/t-1/runs/r-1/join",
};

const WORKRUN = {
  workrun_id: "wr-1",
  tenant_id: "t",
  outcome_type: "invoice_recon",
  status: "awaiting_approval",
  step_label: "phê duyệt",
  intent: "đối soát",
  pending_questions: [
    {
      decision_id: "d-1",
      kind: "approval",
      prompt: "Duyệt kết quả?",
      options: ["approve", "reject"],
    },
  ],
  deliverables: [],
  ingest_errors: [],
  skipped: [],
};

function stubAll(binding: boolean) {
  const calls: string[] = [];
  rs.stubGlobal(
    "fetch",
    rs.fn((u: string) => {
      calls.push(u);
      let body: unknown = {};
      if (u === "/api/ewcp/identity") {
        body = {
          user_id: "u-1",
          actor: "user:u-1",
          user_actor_binding: binding,
        };
      } else if (u === "/api/ewcp/runs/er-1") {
        body = { run: RUN };
      } else if (u === "/api/ewcp/runs/er-1/workrun") {
        body = { workrun: WORKRUN };
      }
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }),
  );
  return calls;
}

describe("ExecutionRunThread", () => {
  test("renders governed truth with decision buttons when binding is on", async () => {
    stubAll(true);
    render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() =>
      expect(screen.getByText("Duyệt kết quả?")).toBeTruthy(),
    );
    const approve = screen.getByText("Duyệt & niêm phong");
    expect(approve.getAttribute("disabled")).toBeNull();
  });

  test("binding off → decisions read-only", async () => {
    stubAll(false);
    render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() =>
      expect(screen.getByText("Duyệt kết quả?")).toBeTruthy(),
    );
    const approve = screen.getByText("Duyệt & niêm phong");
    expect(approve.getAttribute("disabled")).not.toBeNull();
    expect(screen.getByText(/Chỉ xem/)).toBeTruthy();
  });
});
