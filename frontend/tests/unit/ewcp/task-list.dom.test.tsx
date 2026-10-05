import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";

import type { RunView } from "@/ewcp/api";
import { TaskList } from "@/ewcp/components/task-list";

const creds = { apiKey: "", tenant: "demo" };

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function run(id: string, outcomeType: string, intent: string): RunView {
  return {
    workrun_id: id,
    tenant_id: "demo",
    outcome_type: outcomeType,
    status: "verified",
    step_label: "",
    intent,
    pending_questions: [],
    deliverables: [],
    ingest_errors: [],
    skipped: [],
  } as RunView;
}

function stubRuns(items: RunView[]) {
  rs.stubGlobal(
    "fetch",
    rs.fn(async (url: string) => {
      if (url.startsWith("/api/ewcp/workruns")) return jsonResponse(items);
      return jsonResponse({});
    }),
  );
}

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

describe("TaskList outcome_type filter", () => {
  test("no chips when the rail spans a single outcome type", async () => {
    stubRuns([run("w1", "invoice_recon", "a"), run("w2", "invoice_recon", "b")]);
    render(
      <TaskList
        creds={creds}
        activeId={null}
        refreshKey={0}
        onSelect={rs.fn()}
      />,
    );
    expect(await screen.findByText("a")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Tất cả" })).toBeNull();
  });

  test("chips appear with >1 type and filter the rail", async () => {
    stubRuns([
      run("w1", "invoice_recon", "đối soát kỳ 9"),
      run("w2", "dossier_check", "kiểm tra lô hàng"),
    ]);
    render(
      <TaskList
        creds={creds}
        activeId={null}
        refreshKey={0}
        onSelect={rs.fn()}
      />,
    );
    expect(await screen.findByText("đối soát kỳ 9")).toBeTruthy();
    expect(screen.getByText("kiểm tra lô hàng")).toBeTruthy();

    // chip buttons are addressed by role — the same VN label also
    // appears inside each run row's outcome tag
    fireEvent.click(
      screen.getByRole("button", { name: "Đối soát hóa đơn" }),
    );
    expect(screen.getByText("đối soát kỳ 9")).toBeTruthy();
    expect(screen.queryByText("kiểm tra lô hàng")).toBeNull();

    // "Tất cả" restores the unfiltered rail
    fireEvent.click(screen.getByRole("button", { name: "Tất cả" }));
    expect(screen.getByText("kiểm tra lô hàng")).toBeTruthy();

    // clicking the active chip toggles it off too
    fireEvent.click(
      screen.getByRole("button", { name: "Kiểm tra chứng từ" }),
    );
    expect(screen.queryByText("đối soát kỳ 9")).toBeNull();
    fireEvent.click(
      screen.getByRole("button", { name: "Kiểm tra chứng từ" }),
    );
    expect(screen.getByText("đối soát kỳ 9")).toBeTruthy();
  });
});
