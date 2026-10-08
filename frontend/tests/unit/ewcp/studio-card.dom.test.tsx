import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import type { RunView } from "@/ewcp/api";
import { StudioCard } from "@/ewcp/components/studio-card";

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

function runWith(deliverables: RunView["deliverables"]): RunView {
  return {
    workrun_id: "wr-1",
    tenant_id: "t",
    outcome_type: "invoice_recon",
    status: "verified",
    step_label: "",
    intent: "",
    pending_questions: [],
    deliverables,
    ingest_errors: [],
    skipped: [],
  };
}

const DEL = {
  deliverable_id: "d-1",
  kind: "outcome_json",
  sha256: "a".repeat(64),
  uri: "x",
  name: "outcome.json",
};

describe("StudioCard", () => {
  test("renders nothing until deliverables exist", () => {
    const { container } = render(
      <StudioCard run={runWith([])} executionRunId="er-1" />,
    );
    expect(container.textContent).toBe("");
  });

  test("fetches outcome via the execution-run route and renders recon", async () => {
    const calls: string[] = [];
    rs.stubGlobal(
      "fetch",
      rs.fn((u: string) => {
        calls.push(u);
        return Promise.resolve(
          new Response(
            JSON.stringify({
              outcome_type: "invoice_recon",
              result: [
                {
                  status: "MATCHED",
                  source: "a.xml",
                  sources: ["a.xml"],
                  detail: "khớp",
                },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }),
    );
    render(<StudioCard run={runWith([DEL])} executionRunId="er-1" />);
    await waitFor(() =>
      expect(screen.getByText(/Bảng đối soát/)).toBeTruthy(),
    );
    expect(calls[0]).toBe("/api/ewcp/runs/er-1/outcome");
    expect(screen.getByText(/1\/1 khớp/)).toBeTruthy();
  });
});
