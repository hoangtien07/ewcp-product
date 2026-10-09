import { afterEach, describe, expect, rs, test } from "@rstest/core";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import type { ExecutionRun } from "@/ewcp/api";
import { ExecutionRunCard } from "@/ewcp/components/execution-run-card";

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

const run: ExecutionRun = {
  execution_run_id: "er-123456",
  thread_id: "t-1",
  run_id: "r-1",
  workrun_id: "wr-123456",
  task_mode: "governed",
  status: "running",
  intent: "Đối soát hóa đơn kỳ 09",
  idempotency_key: "k",
  created_by: "u-1",
  created_at: "x",
  updated_at: "x",
  join_url: "/api/threads/t-1/runs/r-1/join",
};

describe("ExecutionRunCard", () => {
  test("renders intent, lifecycle status and governed link", () => {
    render(<ExecutionRunCard run={run} active={false} onSelect={rs.fn()} />);
    expect(screen.getByText("Đối soát hóa đơn kỳ 09")).toBeTruthy();
    // the card renders the WP-A6 lifecycle vocabulary (a running run is
    // "accepted" — the work is taken on, nothing claimed yet)
    expect(screen.getByText("Đã tiếp nhận")).toBeTruthy();
    expect(screen.getByText("governed")).toBeTruthy();
    expect(screen.getByText(/wr:wr-12345…/)).toBeTruthy();
  });

  test("pending_interrupt is not presented as complete", () => {
    render(
      <ExecutionRunCard
        run={{ ...run, status: "pending_interrupt" }}
        active={false}
        onSelect={rs.fn()}
      />,
    );
    // still "accepted" — the interrupt waits on a human, not finished
    expect(screen.getByText("Đã tiếp nhận")).toBeTruthy();
    expect(screen.queryByText("Agent hoàn tất")).toBeNull();
  });

  test("click selects the run", () => {
    const onSelect = rs.fn();
    render(<ExecutionRunCard run={run} active={false} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button"));
    expect(onSelect.mock.calls[0]![0]).toEqual(run);
  });
});
