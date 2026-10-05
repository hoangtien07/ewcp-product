import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  createTask,
  decide,
  EwcpError,
  getOutcome,
  listOutcomes,
  listRuns,
  supplyInputs,
  verifyArtifacts,
} from "@/ewcp/api";

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
      files: { invoices_zip: zip },
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

  test("createTask posts spec-declared context keys + input fields generically", async () => {
    const calls = mockFetch(200, { workrun_id: "w" });
    const f = new File(["x"], "any.dat");
    await createTask({
      intent: "kiểm tra chứng từ",
      tenant: "demo",
      apiKey: "",
      files: { some_future_slot: f, empty_slot: null },
      context: { ky: "2025-09", mst_doanh_nghiep: "0101234567" },
    });
    const fd = calls.at(0)?.init?.body as FormData;
    // field names pass through verbatim — the registry owns the schema
    expect(fd.get("some_future_slot")).toBeTruthy();
    expect(fd.get("empty_slot")).toBeNull();
    expect(fd.get("ky")).toBe("2025-09");
    expect(fd.get("mst_doanh_nghiep")).toBe("0101234567");
  });

  test("supplyInputs posts files under their spec field names", async () => {
    const calls = mockFetch(200, { workrun_id: "w9" });
    const f = new File(["x"], "dossier.zip");
    await supplyInputs("w9", {
      tenant: "demo",
      apiKey: "",
      files: { dossier_zip: f },
    });
    const call = calls.at(0);
    expect(call?.url).toBe("/api/ewcp/tasks/w9/inputs");
    const fd = call?.init?.body as FormData;
    expect(fd.get("dossier_zip")).toBeTruthy();
    expect(fd.get("tenant_id")).toBe("demo");
  });

  test("listOutcomes fetches the pack registry with the api key", async () => {
    const calls = mockFetch(200, [
      {
        outcome_type: "invoice_recon",
        description: "d",
        required_checks: ["totals_match"],
        requires_inputs: [
          {
            name: "invoices_zip",
            accept: ".zip",
            label_vn: "Zip hóa đơn",
            required: true,
          },
        ],
      },
    ]);
    const specs = await listOutcomes({ apiKey: "k3" });
    expect(specs[0]?.requires_inputs[0]?.name).toBe("invoices_zip");
    const call = calls.at(0);
    expect(call?.url).toBe("/api/ewcp/outcomes");
    expect(
      (call?.init?.headers as Record<string, string>)["x-ewcp-api-key"],
    ).toBe("k3");
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
      verdict: "PASS",
      manifest_ok: true,
      artifacts: [],
    });
    const ev = new File(["{}"], "evidence.json");
    const art = new File(["x"], "recon.xlsx");
    const res = await verifyArtifacts({ evidenceJson: ev, files: [art] });
    expect(res.verdict).toBe("PASS");
    const call = calls.at(0);
    const fd = call?.init?.body as FormData;
    expect(fd.get("evidence_json")).toBeTruthy();
    expect(fd.getAll("files")).toHaveLength(1);
    // verify is public — no auth header must be set (csrf cookie absent in test env)
    expect(call?.init?.headers).toEqual({});
  });

  test("getOutcome fetches /workruns/{id}/outcome with api key", async () => {
    const calls = mockFetch(200, {
      outcome_type: "dossier_check",
      result: { verdict: "fail", rows: [], documents: [] },
    });
    const res = await getOutcome("wr9", { apiKey: "k2" });
    expect(res.outcome_type).toBe("dossier_check");
    const call = calls.at(0);
    expect(call?.url).toBe("/api/ewcp/workruns/wr9/outcome");
    expect(
      (call?.init?.headers as Record<string, string>)["x-ewcp-api-key"],
    ).toBe("k2");
  });

  test("listRuns unwraps both list and {items} shapes", async () => {
    mockFetch(200, [{ workrun_id: "a" }, { workrun_id: "b" }]);
    const list = await listRuns({ apiKey: "", tenant: "demo" });
    expect(list).toHaveLength(2);
    mockFetch(200, { items: [{ workrun_id: "c" }] });
    const wrapped = await listRuns({ apiKey: "", tenant: "demo" });
    expect(wrapped[0]?.workrun_id).toBe("c");
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
