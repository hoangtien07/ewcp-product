import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import type { RunView } from "@/ewcp/api";
import { ManifestCard } from "@/ewcp/components/manifest-card";
import { ShareVerifyLink } from "@/ewcp/components/share-verify-link";

const HASH = "a".repeat(64);

let clipboardDesc: PropertyDescriptor | undefined;

function stubClipboard(writeText: (text: string) => Promise<void>) {
  clipboardDesc = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
}

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
  if (clipboardDesc) {
    Object.defineProperty(navigator, "clipboard", clipboardDesc);
  } else {
    delete (navigator as { clipboard?: unknown }).clipboard;
  }
});

describe("ShareVerifyLink", () => {
  test("copies the public verify URL and shows the copied state", async () => {
    const writeText = rs.fn((_text: string) => Promise.resolve());
    stubClipboard(writeText);
    render(<ShareVerifyLink manifestHash={HASH} />);
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(screen.getByText("Đã copy link")).toBeTruthy());
    expect(writeText).toHaveBeenCalledTimes(1);
    const url = writeText.mock.calls[0]![0];
    expect(url).toContain(`/verify/${HASH}`);
    // explainer for the recipient is always visible
    expect(screen.getByText(/không cần API key/)).toBeTruthy();
  });

  test("clipboard failure shows the URL for manual copy", async () => {
    stubClipboard(rs.fn(async () => Promise.reject(new Error("denied"))));
    render(<ShareVerifyLink manifestHash={HASH} />);
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() =>
      expect(screen.getByText(/Copy thủ công:/)).toBeTruthy(),
    );
    expect(screen.getByText(/verify/)).toBeTruthy();
    // never claims success it did not get
    expect(screen.queryByText("Đã copy link")).toBeNull();
  });
});

describe("ManifestCard share flow", () => {
  const verifiedRun = {
    workrun_id: "wr-share-1",
    tenant_id: "demo",
    outcome_type: "invoice_recon",
    status: "verified",
    step_label: "",
    intent: "",
    pending_questions: [],
    deliverables: [],
    ingest_errors: [],
    skipped: [],
  } as RunView;

  test("share button appears only after the sealed manifest loads", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => {
        return new Response(
          JSON.stringify({
            workrun_id: "wr-share-1",
            manifest_hash: HASH,
            seal: "sig",
            deliverables: [],
            checks: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }),
    );
    const writeText = rs.fn((_text: string) => Promise.resolve());
    stubClipboard(writeText);
    render(<ManifestCard run={verifiedRun} executionRunId="er-1" />);
    const btn = await screen.findByText("Chia sẻ link kiểm chứng");
    fireEvent.click(btn);
    await waitFor(() => expect(screen.getByText("Đã copy link")).toBeTruthy());
    const url = writeText.mock.calls[0]![0];
    expect(url).toContain(`/verify/${HASH}`);
  });

  test("non-verified run renders no share affordance", () => {
    render(
      <ManifestCard
        run={{ ...verifiedRun, status: "candidate_complete" }}
        executionRunId="er-1"
      />,
    );
    expect(screen.queryByText("Chia sẻ link kiểm chứng")).toBeNull();
  });
});
