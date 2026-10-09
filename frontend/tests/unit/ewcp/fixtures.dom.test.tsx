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
import { ManifestCard } from "@/ewcp/components/manifest-card";

import {
  GOVERNED_EXECUTION_RUN,
  PENDING_QUESTION,
  SEALED_MANIFEST,
  VERIFIED_RUN_VIEW,
} from "./fixtures";

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

function stubFetch(response: unknown = SEALED_MANIFEST) {
  const calls: { url: string; init?: RequestInit }[] = [];
  rs.stubGlobal(
    "fetch",
    rs.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return Promise.resolve(
        new Response(JSON.stringify(response), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }),
  );
  return calls;
}

describe("A6 fixture contract (docs aid)", () => {
  test("ManifestCard renders nothing before kernel status is verified", () => {
    const preVerified = {
      ...VERIFIED_RUN_VIEW,
      status: "candidate_complete",
    } as RunView;
    const { container } = render(
      <ManifestCard
        executionRunId={GOVERNED_EXECUTION_RUN.execution_run_id}
        run={preVerified}
      />,
    );
    expect(container.innerHTML).toBe("");
  });

  test("ManifestCard shows the real sealed hash + verify link once resolved", async () => {
    const calls = stubFetch();
    render(
      <ManifestCard
        executionRunId={GOVERNED_EXECUTION_RUN.execution_run_id}
        run={VERIFIED_RUN_VIEW}
      />,
    );
    await screen.findByText(/KIỂM CHỨNG/);
    expect(calls[0]!.url).toBe(
      `/api/ewcp/runs/${GOVERNED_EXECUTION_RUN.execution_run_id}/manifest`,
    );
    expect(
      screen.getByText(`manifest_hash: ${SEALED_MANIFEST.manifest_hash}`),
    ).toBeTruthy();
    expect(
      screen.getByRole("link", { name: /kiểm chứng/i }).getAttribute("href"),
    ).toBe(`/verify/${SEALED_MANIFEST.manifest_hash}`);
  });

  test("DecisionCard posts the options_v2 slug + decision_id, not the label", async () => {
    const calls = stubFetch(VERIFIED_RUN_VIEW);
    render(
      <DecisionCard
        q={PENDING_QUESTION}
        executionRunId={GOVERNED_EXECUTION_RUN.execution_run_id}
        run={VERIFIED_RUN_VIEW}
        canDecide={true}
        onDone={() => undefined}
      />,
    );
    fireEvent.click(
      screen.getByText("Chấp nhận chênh lệch, ghi chú vào kết quả"),
    );
    await waitFor(() => expect(calls.length).toBe(1));
    expect(calls[0]!.init?.method).toBe("POST");
    expect(JSON.parse(calls[0]!.init!.body as string)).toEqual({
      answer: "accept_variance",
      decision_id: "dec_fixture1",
    });
  });
});
