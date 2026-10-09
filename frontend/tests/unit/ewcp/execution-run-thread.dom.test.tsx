import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import type { ExecutionRun } from "@/ewcp/api";
import { ExecutionRunThread } from "@/ewcp/components/execution-run-thread";

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

const RUN: ExecutionRun = {
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

function stubAll(binding: boolean, run: typeof RUN = RUN) {
  const calls: { url: string; body?: unknown }[] = [];
  rs.stubGlobal(
    "fetch",
    rs.fn((u: string, init?: RequestInit) => {
      calls.push({
        url: u,
        body:
          init?.body && typeof init.body === "string"
            ? JSON.parse(init.body)
            : init?.body,
      });
      let body: unknown = {};
      if (u === "/api/ewcp/identity") {
        body = {
          user_id: "u-1",
          actor: "user:u-1",
          user_actor_binding: binding,
        };
      } else if (u === "/api/ewcp/runs/er-1") {
        body = { run };
      } else if (u === "/api/ewcp/runs/er-1/resume") {
        body = run;
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
  test("thread id renders as a link back to the chat lane", async () => {
    stubAll(true);
    const { container } = render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() =>
      expect(screen.getByText(/thread t-1\.{0,3}/)).toBeTruthy(),
    );
    const link = container.querySelector('a[href="/workspace/chats/t-1"]');
    expect(link).toBeTruthy();
  });

  test("pending_interrupt shows the resume box and posts the payload", async () => {
    const calls = stubAll(true, {
      ...RUN,
      status: "pending_interrupt",
      join_url: null,
    });
    render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() =>
      expect(screen.getByText(/đang chờ câu trả lời/)).toBeTruthy(),
    );
    fireEvent.change(screen.getByPlaceholderText(/Câu trả lời/), {
      target: { value: '{"answer":"approve"}' },
    });
    fireEvent.click(screen.getByText("Tiếp tục"));
    await waitFor(() => {
      const resume = calls.find((c) => c.url.endsWith("/resume"));
      expect(resume?.body).toEqual({ resume: { answer: "approve" } });
    });
  });
});
