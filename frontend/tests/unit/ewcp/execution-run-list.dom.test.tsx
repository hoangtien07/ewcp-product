import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import type { ExecutionRun } from "@/ewcp/api";
import { ExecutionRunList } from "@/ewcp/components/execution-run-list";

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

const RUN: ExecutionRun = {
  execution_run_id: "er-1",
  thread_id: "t-1",
  run_id: "r-1",
  workrun_id: null,
  task_mode: "general",
  status: "completed",
  intent: "khám phá thị trường",
  idempotency_key: "k",
  created_by: "u-1",
  created_at: "x",
  updated_at: "x",
  join_url: null,
};

function stubList(runs: ExecutionRun[]) {
  rs.stubGlobal(
    "fetch",
    rs.fn((_u: string) =>
      Promise.resolve(
        new Response(JSON.stringify({ runs }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ),
  );
}

describe("ExecutionRunList", () => {
  test("renders owner runs from GET /api/ewcp/runs", async () => {
    stubList([RUN]);
    render(<ExecutionRunList onSelect={rs.fn()} />);
    await waitFor(() =>
      expect(screen.getByText("khám phá thị trường")).toBeTruthy(),
    );
    // a completed run with no workrun view reads "agent_finished" in the
    // WP-A6 vocabulary
    expect(screen.getByText("Agent hoàn tất")).toBeTruthy();
  });

  test("empty state", async () => {
    stubList([]);
    render(<ExecutionRunList onSelect={rs.fn()} />);
    await waitFor(() =>
      expect(screen.getByText(/Chưa có ExecutionRun/)).toBeTruthy(),
    );
  });

  test("select emits the run", async () => {
    stubList([RUN]);
    const onSelect = rs.fn();
    render(<ExecutionRunList onSelect={onSelect} />);
    await waitFor(() => screen.getByText("khám phá thị trường"));
    fireEvent.click(screen.getByRole("button"));
    expect(onSelect.mock.calls[0]![0]).toEqual(RUN);
  });
});
