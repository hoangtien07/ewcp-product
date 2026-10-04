import { afterEach, describe, expect, rs, test } from "@rstest/core";

import { createTask, decide, EwcpError, verifyArtifacts } from "@/ewcp/api";

function mockFetch(status: number, body: unknown) {
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
  rs.unstubAllGlobals();
});

describe("ewcp api client", () => {
  test("posts /tasks as multipart with tenant + files", async () => {
    const calls = mockFetch(200, { status: "clarify", clarify_question: "q" });
    const zip = new File(["x"], "inv.zip");
    const res = await createTask({
      intent: "đối soát",
      tenant: "demo",
      apiKey: "k1",
      invoicesZip: zip,
    });
    expect(res.status).toBe("clarify");
    const call = calls.at(0);
    expect(call?.url).toBe("/api/ewcp/tasks");
    const fd = call?.init?.body as FormData;
    expect(fd.get("intent")).toBe("đối soát");
    expect(fd.get("tenant_id")).toBe("demo");
    expect(fd.get("invoices_zip")).toBeTruthy();
    expect(
      (call?.init?.headers as Record<string, string>)["x-ewcp-api-key"],
    ).toBe("k1");
  });

  test("decide posts JSON decision with decision_id", async () => {
    const calls = mockFetch(200, { workrun_id: "w" });
    await decide("wr1", {
      apiKey: "",
      tenant: "demo",
      decidedBy: "boss",
      answer: "approve",
      decisionId: "d9",
    });
    const call = calls.at(0);
    expect(call?.url).toBe("/api/ewcp/workruns/wr1/decisions");
    const body = JSON.parse(call?.init?.body as string);
    expect(body).toEqual({
      tenant_id: "demo",
      decided_by: "boss",
      answer: "approve",
      decision_id: "d9",
    });
  });

  test("verify sends evidence_json + artifact files, no auth needed", async () => {
    const calls = mockFetch(200, {
      ok: true,
      manifest_ok: true,
      artifacts: [],
    });
    const ev = new File(["{}"], "evidence.json");
    const art = new File(["x"], "recon.xlsx");
    const res = await verifyArtifacts({ evidenceJson: ev, files: [art] });
    expect(res.ok).toBe(true);
    const call = calls.at(0);
    const fd = call?.init?.body as FormData;
    expect(fd.get("evidence_json")).toBeTruthy();
    expect(fd.getAll("files")).toHaveLength(1);
    // verify is public — no auth header must be set
    expect(call?.init?.headers).toBeUndefined();
  });

  test("non-2xx raises EwcpError with status + detail", async () => {
    mockFetch(422, { detail: "tenant_id required" });
    await expect(
      createTask({ intent: "x", tenant: "", apiKey: "" }),
    ).rejects.toMatchObject({ status: 422, message: "tenant_id required" });
    await expect(
      createTask({ intent: "x", tenant: "", apiKey: "" }),
    ).rejects.toBeInstanceOf(EwcpError);
  });
});
