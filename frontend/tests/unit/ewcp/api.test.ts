import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  getExecutionRun,
  joinRunStream,
  launchExecutionRun,
  listExecutionRuns,
  listRunsForThread,
  resumeRun,
  submitDecision,
  verifyPermalink,
  verifyShareUrl,
} from "@/ewcp/api";

afterEach(() => {
  rs.unstubAllGlobals();
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const RUN = {
  execution_run_id: "er-1",
  thread_id: "t-1",
  run_id: "r-1",
  workrun_id: "wr-1",
  task_mode: "general",
  status: "running",
  intent: "đối soát",
  idempotency_key: "k-1",
  created_by: "u-1",
  created_at: "2026-01-01",
  updated_at: "2026-01-01",
  join_url: "/api/threads/t-1/runs/r-1/join",
};

describe("ewcp api", () => {
  test("listExecutionRuns unwraps {runs}", async () => {
    const fetchMock = rs.fn((_u: string, _i?: RequestInit) =>
      Promise.resolve(jsonResponse({ runs: [RUN] })),
    );
    rs.stubGlobal("fetch", fetchMock);
    const runs = await listExecutionRuns();
    expect(runs).toEqual([RUN]);
    expect(fetchMock.mock.calls[0]![0]).toBe("/api/ewcp/runs");
  });

  test("getExecutionRun refresh adds query", async () => {
    const fetchMock = rs.fn((_u: string) =>
      Promise.resolve(jsonResponse({ run: RUN })),
    );
    rs.stubGlobal("fetch", fetchMock);
    await getExecutionRun("er-1", { refresh: true });
    expect(fetchMock.mock.calls[0]![0]).toBe("/api/ewcp/runs/er-1?refresh=1");
  });

  test("launchExecutionRun posts multipart intent + mode + files", async () => {
    const fetchMock = rs.fn((_u: string, _i?: RequestInit) =>
      Promise.resolve(jsonResponse({ run: RUN, idempotent_replay: false })),
    );
    rs.stubGlobal("fetch", fetchMock);
    const file = new File([new Uint8Array([1])], "a.txt");
    await launchExecutionRun({
      intent: "đối soát",
      taskMode: "governed",
      files: [file],
      idempotencyKey: "k-9",
    });
    const init = fetchMock.mock.calls[0]![1];
    const fd = init!.body as FormData;
    expect(fd.get("intent")).toBe("đối soát");
    expect(fd.get("task_mode")).toBe("governed");
    expect(fd.get("idempotency_key")).toBe("k-9");
    expect(fd.getAll("files")).toHaveLength(1);
  });

  test("launchExecutionRun governed posts outcome_type + slot-named files", async () => {
    const fetchMock = rs.fn((_u: string, _i?: RequestInit) =>
      Promise.resolve(jsonResponse({ run: RUN, idempotent_replay: false })),
    );
    rs.stubGlobal("fetch", fetchMock);
    await launchExecutionRun({
      intent: "đối soát",
      taskMode: "governed",
      outcomeType: "invoice_recon",
      fields: { mst: "0101" },
      slotFiles: {
        invoices_zip: [new File([new Uint8Array([1])], "inv.zip")],
        books: [new File([new Uint8Array([2])], "s.csv")],
      },
    });
    const fd = fetchMock.mock.calls[0]![1]!.body as FormData;
    expect(fd.get("task_mode")).toBe("governed");
    expect(fd.get("outcome_type")).toBe("invoice_recon");
    expect(fd.get("mst")).toBe("0101");
    expect((fd.get("invoices_zip") as File).name).toBe("inv.zip");
    expect((fd.get("books") as File).name).toBe("s.csv");
    expect(fd.get("files")).toBeNull(); // governed slots never thread-upload
  });

  test("listRunsForThread hits the thread_id filter", async () => {
    const fetchMock = rs.fn((_u: string, _i?: RequestInit) =>
      Promise.resolve(jsonResponse({ runs: [RUN] })),
    );
    rs.stubGlobal("fetch", fetchMock);
    const runs = await listRunsForThread("t-1");
    expect(fetchMock.mock.calls[0]![0]).toBe("/api/ewcp/runs?thread_id=t-1");
    expect(runs).toEqual([RUN]);
  });

  test("resumeRun posts the resume payload as JSON", async () => {
    const fetchMock = rs.fn((_u: string, _i?: RequestInit) =>
      Promise.resolve(jsonResponse(RUN)),
    );
    rs.stubGlobal("fetch", fetchMock);
    await resumeRun("er-1", { answer: "approve" });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/ewcp/runs/er-1/resume");
    expect(JSON.parse(init!.body as string)).toEqual({
      resume: { answer: "approve" },
    });
  });

  test("submitDecision posts answer + decision_id as JSON", async () => {
    const fetchMock = rs.fn((_u: string, _i?: RequestInit) =>
      Promise.resolve(jsonResponse({ workrun_id: "wr-1" })),
    );
    rs.stubGlobal("fetch", fetchMock);
    await submitDecision("er-1", { answer: "approve", decisionId: "d-1" });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/ewcp/runs/er-1/decisions");
    expect(JSON.parse(init!.body as string)).toEqual({
      answer: "approve",
      decision_id: "d-1",
    });
  });

  test("submitDecision retries once on a transient empty 400 (F4)", async () => {
    // E2E F4: the first approve POST can return a bare 400 while the
    // kernel's decision projection catches up — one retry must ride out
    // the race instead of surfacing the failure.
    let calls = 0;
    const fetchMock = rs.fn((_u: string, _i?: RequestInit) => {
      calls += 1;
      return Promise.resolve(
        calls === 1
          ? new Response("", { status: 400 })
          : jsonResponse({ workrun_id: "wr-1", status: "verified" }),
      );
    });
    rs.stubGlobal("fetch", fetchMock);
    const out = await submitDecision("er-1", { answer: "approve" });
    expect(calls).toBe(2);
    expect(out.status).toBe("verified");
  });

  test("submitDecision does not retry a definitive rejection (F4)", async () => {
    const fetchMock = rs.fn((_u: string, _i?: RequestInit) =>
      Promise.resolve(jsonResponse({ detail: "run is running" }, 422)),
    );
    rs.stubGlobal("fetch", fetchMock);
    await expect(
      submitDecision("er-1", { answer: "approve" }),
    ).rejects.toMatchObject({ status: 422 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test("verifyPermalink hits the proxy route", async () => {
    const fetchMock = rs.fn((_u: string) =>
      Promise.resolve(
        jsonResponse({ workrun_id: "w", seal_ok: true, manifest: {} }),
      ),
    );
    rs.stubGlobal("fetch", fetchMock);
    await verifyPermalink("h-1");
    expect(fetchMock.mock.calls[0]![0]).toBe("/api/ewcp/verify/h-1");
  });

  test("verifyShareUrl points at /verify/<hash>", () => {
    expect(verifyShareUrl("h-2")).toContain("/verify/h-2");
  });

  test("joinRunStream parses SSE event/data frames", async () => {
    const payload = 'event: values\ndata: {"a":1}\n\nevent: end\ndata: {}\n\n';
    const body = new ReadableStream({
      start(c) {
        c.enqueue(new TextEncoder().encode(payload));
        c.close();
      },
    });
    rs.stubGlobal(
      "fetch",
      rs.fn((_u: string, _i?: RequestInit) =>
        Promise.resolve(new Response(body, { status: 200 })),
      ),
    );
    const events: { event: string; data: unknown }[] = [];
    await joinRunStream("/api/threads/t-1/runs/r-1/join", {
      onEvent: (e) => events.push(e),
    });
    expect(events).toEqual([
      { event: "values", data: { a: 1 } },
      { event: "end", data: {} },
    ]);
  });

  test("joinRunStream surfaces a server gap frame verbatim", async () => {
    // N04 (b): a resuming consumer's `stream_replay_gap` must reach the
    // handler as its own event — not be dropped or blended into a data
    // frame — so the caller can mark the replay boundary.
    const payload =
      'event: gap\ndata: {"code":"stream_replay_gap","requested_event_id":"1-0","recovery":"reload_durable_state"}\n\n';
    const body = new ReadableStream({
      start(c) {
        c.enqueue(new TextEncoder().encode(payload));
        c.close();
      },
    });
    rs.stubGlobal(
      "fetch",
      rs.fn((_u: string, _i?: RequestInit) =>
        Promise.resolve(new Response(body, { status: 200 })),
      ),
    );
    const events: { event: string; data: unknown }[] = [];
    await joinRunStream("/api/threads/t-1/runs/r-1/join", {
      onEvent: (e) => events.push(e),
    });
    expect(events).toEqual([
      {
        event: "gap",
        data: {
          code: "stream_replay_gap",
          requested_event_id: "1-0",
          recovery: "reload_durable_state",
        },
      },
    ]);
  });

  test("joinRunStream throws EwcpError on non-2xx", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn((_u: string) =>
        Promise.resolve(new Response("nope", { status: 404 })),
      ),
    );
    await expect(joinRunStream("/x")).rejects.toMatchObject({ status: 404 });
  });
});
