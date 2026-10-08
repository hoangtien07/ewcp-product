import { afterEach, describe, expect, rs, test } from "@rstest/core";

import { handoffToGeneralLane } from "@/ewcp/exploratory";

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("handoffToGeneralLane", () => {
  test("launches a general-lane ExecutionRun with the intent", async () => {
    const run = {
      execution_run_id: "er-9",
      thread_id: "t-9",
      run_id: "r-9",
      workrun_id: null,
      task_mode: "general",
      status: "running",
      intent: "khám phá thị trường",
      idempotency_key: "k",
      created_by: "u-1",
      created_at: "x",
      updated_at: "x",
      join_url: "/api/threads/t-9/runs/r-9/join",
    };
    const fetchMock = rs.fn((_u: string, _i?: RequestInit) =>
      Promise.resolve(
        new Response(JSON.stringify({ run, idempotent_replay: false }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    rs.stubGlobal("fetch", fetchMock);
    const got = await handoffToGeneralLane("khám phá thị trường", {
      idempotencyKey: "k-1",
    });
    expect(got).toEqual(run);
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/api/ewcp/runs");
    const fd = init!.body as FormData;
    expect(fd.get("task_mode")).toBe("general");
    expect(fd.get("intent")).toBe("khám phá thị trường");
    expect(fd.get("idempotency_key")).toBe("k-1");
  });
});
