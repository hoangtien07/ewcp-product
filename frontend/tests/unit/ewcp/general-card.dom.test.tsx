import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import type { RunView } from "@/ewcp/api";
import { GeneralCard } from "@/ewcp/components/general-card";

// M-EA1 general lane — the card surfaces the kernel-exposed TOP-LEVEL
// `run.general` projection (phase, attempt, contract_version,
// state_warnings, rejections — spec 005 §2.4) plus the outcome JSON the
// sealed spec serves. Pre-merge spec text placed the projection under
// context.general — the card reads that as a defensive fallback.

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const baseRun = {
  workrun_id: "wr-gen1",
  tenant_id: "demo",
  outcome_type: "general",
  status: "awaiting_input",
  step_label: "Chờ duyệt tiêu chí",
  intent: "sửa master workbook khớp 28 chi nhánh",
  pending_questions: [],
  deliverables: [],
  ingest_errors: [],
  skipped: [],
} as RunView;

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

describe("GeneralCard", () => {
  test("renders the kernel wire shape — top-level general with flat fields", () => {
    // kernel _run_view emits run.general verbatim: phase, attempt,
    // contract_version, state_warnings, rejections — NOT context.general
    const run = {
      ...baseRun,
      general: {
        phase: "execute",
        attempt: 2,
        contract_version: 1,
        state_warnings: ["state.json thiếu trường plan"],
        rejections: [
          { path: "ewcp/outbox/999-bad.json", reason: "absolute path" },
        ],
      },
    } as RunView;
    render(<GeneralCard run={run} apiKey="" />);
    expect(screen.getByText("Lane tổng quát")).toBeTruthy();
    expect(screen.getByText(/đang thực thi/)).toBeTruthy();
    expect(screen.getByText(/lượt 2/)).toBeTruthy();
    expect(screen.getByText(/Tiêu chí nghiệm thu v1/)).toBeTruthy();
    expect(screen.getByText(/đã duyệt/)).toBeTruthy();
    expect(screen.getByText(/state\.json thiếu trường plan/)).toBeTruthy();
    expect(screen.getByText(/999-bad\.json/)).toBeTruthy();
  });

  test("renders the TaskState projection from context.general", () => {
    const run = {
      ...baseRun,
      context: {
        general: {
          goal: { statement: "sửa master workbook khớp 28 chi nhánh" },
          plan: [
            { step: "đọc totals.csv làm baseline", status: "done" },
            { step: "scan 28 branch files", status: "doing" },
            { step: "write mismatch list", status: "todo" },
          ],
          facts: [{ k: "n_branch_files", v: "28" }],
          open_questions: ["master.xlsx sheet consolidated có 2 dòng trùng sku?"],
          blockers: [],
        },
      },
    } as RunView;
    render(<GeneralCard run={run} apiKey="" />);
    expect(screen.getByText("Lane tổng quát")).toBeTruthy();
    expect(
      screen.getByText("sửa master workbook khớp 28 chi nhánh"),
    ).toBeTruthy();
    expect(screen.getByText("đọc totals.csv làm baseline")).toBeTruthy();
    expect(screen.getByText("scan 28 branch files")).toBeTruthy();
    expect(screen.getByText("write mismatch list")).toBeTruthy();
    expect(screen.getByText(/n_branch_files = 28/)).toBeTruthy();
    expect(screen.getByText(/2 dòng trùng sku/)).toBeTruthy();
  });

  test("renders the contract summary when context carries it", () => {
    const run = {
      ...baseRun,
      context: {
        general: {
          contract: {
            version: 2,
            criteria_count: 4,
            proposed_by: "agent",
            approved_by: "d-9",
          },
        },
      },
    } as RunView;
    render(<GeneralCard run={run} apiKey="" />);
    expect(screen.getByText(/Tiêu chí nghiệm thu/)).toBeTruthy();
    expect(screen.getByText(/v2/)).toBeTruthy();
    expect(screen.getByText(/đã duyệt/)).toBeTruthy();
  });

  test("fetches and pretty-prints the outcome JSON once deliverables exist", async () => {
    const run = {
      ...baseRun,
      status: "verified",
      context: { general: { goal: "fix workbook" } },
      deliverables: [
        {
          deliverable_id: "d1",
          kind: "result",
          sha256: "abc",
          uri: "outputs/outcome.json",
          name: "outcome.json",
        },
      ],
    } as RunView;
    rs.stubGlobal(
      "fetch",
      rs.fn(async (url: string) => {
        if (url === "/api/ewcp/workruns/wr-gen1/outcome") {
          return jsonResponse({
            outcome_type: "general",
            result: { summary: "28/28 khớp", ok: true },
          });
        }
        return jsonResponse({});
      }),
    );
    render(<GeneralCard run={run} apiKey="" />);
    await waitFor(() =>
      expect(screen.getByText("Kết quả outcome (JSON)")).toBeTruthy(),
    );
    fireEvent.click(screen.getByText("Kết quả outcome (JSON)"));
    expect(screen.getByText(/28\/28 khớp/)).toBeTruthy();
  });

  test("renders nothing when the kernel exposes no general context and no outcome", () => {
    const { container } = render(
      <GeneralCard run={baseRun} apiKey="" />,
    );
    expect(container.firstChild).toBeNull();
  });
});
