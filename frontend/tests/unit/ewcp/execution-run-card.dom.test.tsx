import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

import type { ExecutionRun } from "@/ewcp/api";
import {
  ExecutionRunCard,
  runStatusLabel,
} from "@/ewcp/components/execution-run-card";

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
    expect(screen.getByText("Đang chạy")).toBeTruthy();
    expect(screen.getByText("governed")).toBeTruthy();
    expect(screen.getByText(/wr:wr-12345…/)).toBeTruthy();
  });

  test("pending_interrupt is not presented as complete", () => {
    expect(runStatusLabel("pending_interrupt")).toBe("Chờ thao tác");
    expect(runStatusLabel("completed")).toBe("Hoàn tất");
  });

  test("click selects the run", () => {
    const onSelect = rs.fn();
    render(<ExecutionRunCard run={run} active={false} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button"));
    expect(onSelect.mock.calls[0]![0]).toEqual(run);
  });
});
