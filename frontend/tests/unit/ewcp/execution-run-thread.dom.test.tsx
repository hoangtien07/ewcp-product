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

function stubAll(
  binding: boolean,
  run: typeof RUN = RUN,
  workrun: typeof WORKRUN = WORKRUN,
  joinBody = "event: end\ndata: null\n\n",
  extraRuns: Record<string, typeof RUN> = {},
) {
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
      } else if (u === "/api/ewcp/runs/er-1?refresh=1") {
        body = { run };
      } else if (/^\/api\/ewcp\/runs\/[^/?]+$/.test(u)) {
        const other = extraRuns[u.slice("/api/ewcp/runs/".length)];
        if (other) body = { run: other };
      } else if (/^\/api\/ewcp\/runs\/[^/?]+\/workrun$/.test(u)) {
        body = { workrun };
      } else if (u === "/api/threads/t-1/runs/r-1/join") {
        return Promise.resolve(
          new Response(joinBody, {
            status: 200,
            headers: { "Content-Type": "text/event-stream" },
          }),
        );
      } else if (u === "/api/ewcp/runs/er-1/decisions") {
        // the kernel decision POST returns the workrun run-view itself
        body = workrun;
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

  test("clean candidate_complete renders a seal approval gate (decision_id null)", async () => {
    const calls = stubAll(true, RUN, {
      ...WORKRUN,
      status: "candidate_complete",
      pending_questions: [],
    });
    render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() =>
      expect(screen.getByText("Duyệt & niêm phong")).toBeTruthy(),
    );
    fireEvent.click(screen.getByText("Duyệt & niêm phong"));
    await waitFor(() => {
      const decision = calls.find((c) => c.url.endsWith("/decisions"));
      expect(decision?.body).toEqual({
        answer: "approve",
        decision_id: null,
      });
    });
  });

  test("an emitted approval question wins over the synthesized gate", async () => {
    const calls = stubAll(true, RUN, {
      ...WORKRUN,
      status: "candidate_complete",
    });
    render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() =>
      expect(screen.getByText("Duyệt kết quả?")).toBeTruthy(),
    );
    // the emitted question's own prompt renders; no duplicate gate card
    expect(screen.queryByText(/niêm phong kết quả \(manifest/)).toBeNull();
    fireEvent.click(screen.getByText("Duyệt & niêm phong"));
    await waitFor(() => {
      const decision = calls.find((c) => c.url.endsWith("/decisions"));
      expect(decision?.body).toEqual({
        answer: "approve",
        decision_id: "d-1",
      });
    });
  });

  test("stream end on a live governed run re-fetches the workrun, not only the record", async () => {
    const calls = stubAll(
      true,
      { ...RUN, status: "running" },
      { ...WORKRUN, status: "candidate_complete" },
    );
    render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() => {
      const workrunReads = calls.filter(
        (c) => c.url === "/api/ewcp/runs/er-1/workrun",
      );
      expect(workrunReads.length).toBeGreaterThanOrEqual(2);
    });
    // and the refreshed governed truth renders, not the mount-time state
    await waitFor(() =>
      expect(screen.getByText("Duyệt kết quả?")).toBeTruthy(),
    );
  });

  // --- N04 — SSE recovery honesty ------------------------------------------
  // (a) The stream dies mid-run with NO terminal frame: the pane refreshes
  // the record once, then stops observing — today it shows the last-known
  // status as a settled display with no drop marker.
  test("stream drop mid-run keeps showing the last-known status (pinned)", async () => {
    const calls = stubAll(
      true,
      { ...RUN, status: "running" },
      { ...WORKRUN, status: "running", pending_questions: [] },
      "event: values\ndata: {}\n\n", // EOF — no `end`/`error` frame
    );
    const { container } = render(<ExecutionRunThread executionRunId="er-1" />);
    // the stream ended and the one-shot refresh ran
    await waitFor(() =>
      expect(calls.some((c) => c.url === "/api/ewcp/runs/er-1?refresh=1")).toBe(
        true,
      ),
    );
    await waitFor(() => expect(screen.getByText("Đã tiếp nhận")).toBeTruthy());
    // pinned: the badge is the last-known durable record (literally true),
    // and no live indicator remains in the header — pre-fix this settled
    // display was indistinguishable from a clean end (the N04 dishonesty).
    const header = container.querySelector("header");
    expect(header?.textContent).not.toContain("trực tiếp");
  });

  test("stream drop mid-run marks the live observation as lost", async () => {
    stubAll(
      true,
      { ...RUN, status: "running" },
      { ...WORKRUN, status: "running", pending_questions: [] },
      "event: values\ndata: {}\n\n",
    );
    render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() =>
      expect(screen.getByText(/Mất kết nối trực tiếp/)).toBeTruthy(),
    );
    // the last-known record still renders — the marker annotates, not hides
    expect(screen.getByText("Đã tiếp nhận")).toBeTruthy();
  });

  // Council rework (W9): the pane is mounted once and `executionRunId`
  // switches re-use the same instance (no `key`) — a `streamLost` marker set
  // on run A must not survive onto run B's detail.
  test("a stream-lost marker does not leak onto the next run's detail", async () => {
    stubAll(
      true,
      { ...RUN, status: "running" },
      { ...WORKRUN, status: "running", pending_questions: [] },
      "event: values\ndata: {}\n\n",
      {
        "er-2": {
          ...RUN,
          execution_run_id: "er-2",
          run_id: "r-2",
          status: "completed",
          join_url: "/api/threads/t-1/runs/r-2/join",
        },
      },
    );
    const { rerender } = render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() =>
      expect(screen.getByText(/Mất kết nối trực tiếp/)).toBeTruthy(),
    );
    rerender(<ExecutionRunThread executionRunId="er-2" />);
    // er-2 loads async; the marker must clear once its record lands
    await waitFor(() =>
      expect(screen.queryByText(/Mất kết nối trực tiếp/)).toBeNull(),
    );
    expect(screen.getByText("Đã tiếp nhận")).toBeTruthy();
  });

  test("a clean `end` frame does not claim a dropped stream", async () => {
    stubAll(
      true,
      { ...RUN, status: "running" },
      { ...WORKRUN, status: "candidate_complete" },
      "event: values\ndata: {}\n\nevent: end\ndata: {}\n\n",
    );
    render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() =>
      expect(screen.getByText("Duyệt kết quả?")).toBeTruthy(),
    );
    expect(screen.queryByText(/Mất kết nối trực tiếp/)).toBeNull();
  });

  // (b) Server `gap` frames must not blend into the feed as ordinary events
  // — pin that the raw marker reaches the user-facing log.
  test("a server gap frame surfaces in the activity feed", async () => {
    stubAll(
      true,
      { ...RUN, status: "running" },
      { ...WORKRUN, status: "running", pending_questions: [] },
      'event: gap\ndata: {"code":"stream_replay_gap","recovery":"reload_durable_state"}\n\n',
    );
    render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() =>
      expect(screen.getByText(/stream_replay_gap/)).toBeTruthy(),
    );
  });

  // (b′) The pane never sends Last-Event-ID, so its own recovery case is a
  // tail-only replay after eviction — pinned as silent truncation today
  // (documented cosmetic limit in docs/vnext/A7A_SSE_RECONNECT.md).
  test("tail-only replay renders contiguously with no truncation marker (pinned)", async () => {
    stubAll(
      true,
      { ...RUN, status: "running" },
      { ...WORKRUN, status: "running", pending_questions: [] },
      'event: values\ndata: {"seq":5}\n\nevent: values\ndata: {"seq":6}\n\nevent: end\ndata: {}\n\n',
    );
    render(<ExecutionRunThread executionRunId="er-1" />);
    // the `end` frame itself renders too — 2 tail values + end, contiguous
    await waitFor(() =>
      expect(screen.getByText(/Hoạt động agent \(3 sự kiện\)/)).toBeTruthy(),
    );
    expect(screen.queryByText(/stream_replay_gap/)).toBeNull();
  });

  // (c) Kernel truth vs the launcher record: a completed product run whose
  // kernel workrun still runs must not claim completion.
  test("a completed launcher record with a still-running kernel workrun claims no completion", async () => {
    stubAll(true, RUN /* completed */, {
      ...WORKRUN,
      status: "running",
      pending_questions: [],
    });
    render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() => expect(screen.getByText("Đã tiếp nhận")).toBeTruthy());
    expect(screen.getByText(/Đang xử lý/)).toBeTruthy();
    expect(screen.queryByText("Agent hoàn tất")).toBeNull();
  });

  test("a completed launcher record with a failed kernel workrun shows UNKNOWN, not finished", async () => {
    stubAll(true, RUN /* completed */, {
      ...WORKRUN,
      status: "failed",
      pending_questions: [],
    });
    render(<ExecutionRunThread executionRunId="er-1" />);
    await waitFor(() => expect(screen.getByText("Không rõ")).toBeTruthy());
    expect(screen.getByText(/Lỗi/)).toBeTruthy();
    expect(screen.queryByText("Agent hoàn tất")).toBeNull();
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
