import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { TaskThread } from "@/ewcp/components/task-thread";

const routerMock = { push: rs.fn(), replace: rs.fn(), refresh: rs.fn() };
rs.mock("next/navigation", () => ({
  useRouter: () => routerMock,
  usePathname: () => "/ewcp",
  useSearchParams: () => new URLSearchParams(),
}));

const creds = { apiKey: "", tenant: "demo", decidedBy: "boss" };

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  cleanup();
  rs.unstubAllGlobals();
  routerMock.push.mockClear();
  window.sessionStorage.clear();
});

describe("TaskThread clarify handoff", () => {
  test("clarify card hands the intent to the lead_agent lane and navigates", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    rs.stubGlobal(
      "fetch",
      rs.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, init });
        if (url === "/api/ewcp/tasks") {
          return jsonResponse({
            status: "clarify",
            clarify_question: "Kỳ nào?",
          });
        }
        if (url.includes("/auth/me")) {
          return jsonResponse({ id: "u1" });
        }
        return jsonResponse({ thread_id: "t" });
      }),
    );
    const onHandoff = rs.fn();
    render(
      <TaskThread
        creds={creds}
        run={null}
        onRun={rs.fn()}
        onHandoff={onHandoff}
      />,
    );

    fireEvent.change(
      screen.getByPlaceholderText(/Yêu cầu nghiệp vụ/),
      { target: { value: "đối soát hóa đơn" } },
    );
    fireEvent.click(screen.getByText("Gửi yêu cầu"));

    const handoffBtn = await screen.findByText(
      "Chuyển sang trợ lý tổng quát",
    );
    fireEvent.click(handoffBtn);

    await waitFor(() => expect(routerMock.push).toHaveBeenCalled());
    // lane-side thread materialized through the upstream REST route
    expect(
      calls.some((c) => c.url === "/api/threads" && c.init?.method === "POST"),
    ).toBe(true);
    expect(onHandoff).toHaveBeenCalledTimes(1);

    const pushed = routerMock.push.mock.calls.at(0)?.at(0) as string;
    const threadId = pushed.replace("/workspace/chats/", "");
    expect(threadId).toMatch(/^[0-9a-f-]{36}$/);

    // composer draft prefilled on the key the chat page will read
    const draft = JSON.parse(
      window.sessionStorage.getItem(
        `deerflow:composer-draft:v1:u1:lead-agent:${threadId}`,
      ) ?? "null",
    );
    expect(draft.text).toContain("đối soát hóa đơn");
    expect(draft.text).toContain("Kỳ nào?");

    // handoff recorded for the UNVERIFIED lane section
    const handoffs = JSON.parse(
      window.sessionStorage.getItem("ewcp_exploratory_handoffs") ?? "[]",
    );
    expect(handoffs.at(0)?.thread_id).toBe(threadId);
  });
});
