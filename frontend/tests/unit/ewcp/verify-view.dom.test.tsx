import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { VerifyView } from "@/ewcp/components/verify-view";

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

const HASH = "b".repeat(64);

function stubPermalink(sealOk: boolean | null) {
  rs.stubGlobal(
    "fetch",
    rs.fn((_u: string) =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            workrun_id: "wr-1",
            seal_ok: sealOk,
            manifest: { manifest_hash: HASH, deliverables: [], checks: [] },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    ),
  );
}

describe("VerifyView", () => {
  test("initial hash auto-looks-up the permalink", async () => {
    stubPermalink(true);
    render(<VerifyView initialHash={HASH} />);
    await waitFor(() =>
      expect(screen.getByText(/PASS — niêm phong hợp lệ/)).toBeTruthy(),
    );
  });

  test("seal mismatch renders FAIL", async () => {
    stubPermalink(false);
    render(<VerifyView initialHash={HASH} />);
    await waitFor(() =>
      expect(screen.getByText(/FAIL — seal không khớp/)).toBeTruthy(),
    );
  });

  test("null seal renders the unprovable state", async () => {
    stubPermalink(null);
    render(<VerifyView initialHash={HASH} />);
    await waitFor(() =>
      expect(
        screen.getByText(/MANIFEST TỒN TẠI — seal không chứng minh được/),
      ).toBeTruthy(),
    );
  });

  test("manual lookup hits /api/ewcp/verify/<hash>", async () => {
    const calls: string[] = [];
    rs.stubGlobal(
      "fetch",
      rs.fn((u: string) => {
        calls.push(u);
        return Promise.resolve(
          new Response(
            JSON.stringify({ workrun_id: "w", seal_ok: true, manifest: {} }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }),
    );
    render(<VerifyView />);
    fireEvent.change(screen.getByPlaceholderText(/manifest_hash/), {
      target: { value: HASH },
    });
    fireEvent.click(screen.getByText("Tra cứu"));
    await waitFor(() => expect(calls.length).toBe(1));
    expect(calls[0]).toBe(`/api/ewcp/verify/${HASH}`);
  });
});
