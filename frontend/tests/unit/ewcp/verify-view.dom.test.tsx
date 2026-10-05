import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { VerifyView } from "@/ewcp/components/verify-view";

const HASH = "b".repeat(64);

function permalinkBody(sealOk: boolean | null = true) {
  return {
    workrun_id: "wr-permalink-1",
    seal_ok: sealOk,
    manifest: {
      workrun_id: "wr-permalink-1",
      manifest_hash: HASH,
      seal: "sig-hex",
      checks: [
        {
          name: "human_approval",
          result: "PASS",
          detail: "approved by sếp",
          validator_ref: "core",
          evidence_hash: "e".repeat(64),
        },
      ],
      deliverables: [
        {
          deliverable_id: "d1",
          kind: "xlsx",
          sha256: "c".repeat(64),
          name: "ket_qua_doi_soat.xlsx",
        },
      ],
    },
  };
}

function fetchStub(body: unknown = permalinkBody(), status = 200) {
  const calls: { url: string; init?: RequestInit }[] = [];
  rs.stubGlobal(
    "fetch",
    rs.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
});

describe("VerifyView permalink lane (?manifest=)", () => {
  test("auto-fetches GET /verify/{hash} — no auth header — and renders PASS", async () => {
    const calls = fetchStub();
    render(<VerifyView initialManifest={HASH} />);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.url).toBe(`/api/ewcp/verify/${HASH}`);
    // public: the fetch carries no headers/init at all
    expect(calls[0]?.init).toBeUndefined();
    await screen.findByText("PASS — niêm phong hợp lệ");
    expect(screen.getByText("ket_qua_doi_soat.xlsx")).toBeTruthy();
    expect(screen.getByText("human_approval")).toBeTruthy();
    // chains into lane 2: workrun_id prefilled for evidence download
    const runInput =
      screen.getByPlaceholderText<HTMLInputElement>("workrun_id");
    expect(runInput.value).toBe("wr-permalink-1");
  });

  test("seal_ok=false renders FAIL — seal không khớp", async () => {
    fetchStub(permalinkBody(false));
    render(<VerifyView initialManifest={HASH} />);
    await screen.findByText("FAIL — seal không khớp");
  });

  test("seal_ok=null renders the unprovable-seal state", async () => {
    fetchStub(permalinkBody(null));
    render(<VerifyView initialManifest={HASH} />);
    await screen.findByText(/seal không chứng minh được/);
  });

  test("unknown hash surfaces the kernel error", async () => {
    fetchStub({ detail: "unknown manifest hash" }, 404);
    render(<VerifyView initialManifest={HASH} />);
    await screen.findByText(/unknown manifest hash/);
  });

  test("no manifest param → no auto fetch; manual hash + Tra cứu works", async () => {
    const calls = fetchStub();
    render(<VerifyView />);
    expect(calls).toHaveLength(0);
    fireEvent.change(
      screen.getByPlaceholderText(/manifest_hash/),
      { target: { value: HASH } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Tra cứu" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]?.url).toBe(`/api/ewcp/verify/${HASH}`);
    await screen.findByText("PASS — niêm phong hợp lệ");
  });

  test("fetch is not issued before the user clicks Tra cứu", () => {
    const calls = fetchStub();
    render(<VerifyView />);
    fireEvent.change(
      screen.getByPlaceholderText(/manifest_hash/),
      { target: { value: HASH } },
    );
    expect(calls).toHaveLength(0);
  });
});
