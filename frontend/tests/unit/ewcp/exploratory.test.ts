import { afterEach, describe, expect, rs, test } from "@rstest/core";

import {
  handoffThreadPath,
  handoffToGeneralLane,
  readHandoffs,
  recordHandoff,
} from "@/ewcp/exploratory";

function fakeStorage() {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
}

function mockHandoffFetch(meBody: unknown, meStatus = 200) {
  const calls: { url: string; init?: RequestInit }[] = [];
  rs.stubGlobal(
    "fetch",
    rs.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      const body = url.includes("/auth/me") ? meBody : { thread_id: "t" };
      return new Response(JSON.stringify(body), {
        status: url.includes("/auth/me") ? meStatus : 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

afterEach(() => {
  rs.unstubAllGlobals();
});

describe("handoff storage", () => {
  test("readHandoffs returns [] on empty/corrupt storage", () => {
    const storage = fakeStorage();
    expect(readHandoffs(storage)).toEqual([]);
    storage.map.set("ewcp_exploratory_handoffs", "{not json");
    expect(readHandoffs(storage)).toEqual([]);
    storage.map.set("ewcp_exploratory_handoffs", "[{bad:1}]");
    expect(readHandoffs(storage)).toEqual([]);
  });

  test("recordHandoff prepends, dedups by thread_id, and persists", () => {
    const storage = fakeStorage();
    const h1 = { thread_id: "t1", intent: "a", at: "2026-01-01T00:00:00Z" };
    const h2 = { thread_id: "t2", intent: "b", at: "2026-01-02T00:00:00Z" };
    recordHandoff(h1, storage);
    const list = recordHandoff(h2, storage);
    expect(list.map((h) => h.thread_id)).toEqual(["t2", "t1"]);
    recordHandoff(h1, storage);
    expect(readHandoffs(storage).map((h) => h.thread_id)).toEqual([
      "t1",
      "t2",
    ]);
  });
});

describe("handoffToGeneralLane", () => {
  test("creates the lane thread, prefills its composer draft, records handoff", async () => {
    const calls = mockHandoffFetch({ id: "u1" });
    const storage = fakeStorage();
    const h = await handoffToGeneralLane({
      intent: "đối soát hóa đơn",
      clarifyQuestion: "Kỳ nào?",
      storage,
    });
    expect(h.thread_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(h.intent).toBe("đối soát hóa đơn");

    // upstream createThread rides the same /api/threads route
    const threadCall = calls.find((c) => c.url === "/api/threads");
    expect(threadCall?.init?.method).toBe("POST");
    expect(JSON.parse(threadCall?.init?.body as string)).toEqual({
      thread_id: h.thread_id,
    });

    // draft lands on the exact key the chat composer reads
    const key = `deerflow:composer-draft:v1:u1:lead-agent:${h.thread_id}`;
    const draft = JSON.parse(storage.map.get(key) ?? "null");
    expect(draft.text).toContain("đối soát hóa đơn");
    expect(draft.text).toContain("Kỳ nào?");
    expect(draft.skillName).toBeNull();

    expect(readHandoffs(storage).at(0)?.thread_id).toBe(h.thread_id);
  });

  test("falls back to the anonymous draft key when /me is unavailable", async () => {
    mockHandoffFetch({ detail: "unauthorized" }, 401);
    const storage = fakeStorage();
    const h = await handoffToGeneralLane({ intent: "x", storage });
    const key = `deerflow:composer-draft:v1:anonymous:lead-agent:${h.thread_id}`;
    expect(storage.map.has(key)).toBe(true);
  });
});

describe("handoffThreadPath", () => {
  test("routes to the lead_agent chat lane", () => {
    expect(handoffThreadPath("abc")).toBe("/workspace/chats/abc");
  });
});
