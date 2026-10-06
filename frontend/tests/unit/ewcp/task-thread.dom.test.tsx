import { afterEach, describe, expect, rs, test } from "@rstest/core";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import type { RunView } from "@/ewcp/api";
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

  test("clarify with assist_available:false keeps the exploratory note", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async (url: string) => {
        if (url === "/api/ewcp/tasks") {
          return jsonResponse({
            status: "clarify",
            clarify_question: "Kỳ nào?",
            assist_available: false,
          });
        }
        return jsonResponse({});
      }),
    );
    render(<TaskThread creds={creds} run={null} onRun={rs.fn()} />);
    fireEvent.change(screen.getByPlaceholderText(/Yêu cầu nghiệp vụ/), {
      target: { value: "đối soát hóa đơn" },
    });
    fireEvent.click(screen.getByText("Gửi yêu cầu"));
    expect(
      await screen.findByText(/Lane khám phá không governed/),
    ).toBeTruthy();
    expect(screen.queryByText(/lane tổng quát governed/i)).toBeNull();
    // the exploratory handoff button is still offered
    expect(
      screen.getByText("Chuyển sang trợ lý tổng quát"),
    ).toBeTruthy();
  });

  test("clarify with assist_available:true notes the governed lane exists", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async (url: string) => {
        if (url === "/api/ewcp/tasks") {
          return jsonResponse({
            status: "clarify",
            clarify_question: "Kỳ nào?",
            assist_available: true,
          });
        }
        return jsonResponse({});
      }),
    );
    render(<TaskThread creds={creds} run={null} onRun={rs.fn()} />);
    fireEvent.change(screen.getByPlaceholderText(/Yêu cầu nghiệp vụ/), {
      target: { value: "đối soát hóa đơn" },
    });
    fireEvent.click(screen.getByText("Gửi yêu cầu"));
    expect(
      await screen.findByText(/lane tổng quát governed đã sẵn sàng/i),
    ).toBeTruthy();
  });
});

describe("TaskThread spec-driven intake", () => {
  const baseRun = {
    workrun_id: "wr1",
    tenant_id: "demo",
    outcome_type: "invoice_recon",
    status: "awaiting_input",
    step_label: "Chờ thêm đầu vào",
    intent: "x",
    pending_questions: [
      {
        decision_id: "d1",
        kind: "missing_input",
        prompt: "Thiếu đầu vào: books",
        options: ["provided"],
      },
    ],
    deliverables: [],
    ingest_errors: [],
    skipped: [],
  } as RunView;

  test("slots + optional markers render from GET /outcomes, not hardcode", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async (url: string) => {
        if (url === "/api/ewcp/outcomes") {
          return jsonResponse([
            {
              outcome_type: "new_pack",
              description: "pack mới",
              required_checks: [],
              requires_inputs: [
                {
                  name: "archive_zip",
                  accept: ".zip",
                  label_vn: "Zip tuỳ chỉnh",
                  required: true,
                },
                {
                  name: "extra_csv",
                  accept: ".csv",
                  label_vn: "File phụ",
                  required: false,
                },
              ],
            },
          ]);
        }
        return jsonResponse({});
      }),
    );
    render(<TaskThread creds={creds} run={null} onRun={rs.fn()} />);

    // spec-declared slots appear (slot + gallery card each render the
    // label); hardcoded pack slots disappear
    expect(
      (await screen.findAllByText("Zip tuỳ chỉnh")).length,
    ).toBeGreaterThanOrEqual(1);
    expect(
      screen.getAllByText("File phụ (tuỳ chọn)").length,
    ).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText(/Sổ kế toán/)).toBeNull();
    // router hint + gallery card both name the registered pack; an
    // unmapped outcome_type passes through verbatim
    expect(screen.getAllByText("new_pack").length).toBeGreaterThanOrEqual(
      2,
    );
    // no DEMO_PRESETS entry for this pack -> no sample-data row at all
    expect(screen.queryByText("Dữ liệu mẫu:")).toBeNull();
  });

  test("registry fetch failure falls back to the bundled pack slots", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => {
        throw new Error("offline");
      }),
    );
    render(<TaskThread creds={creds} run={null} onRun={rs.fn()} />);
    // FALLBACK_SPECS: dossier_zip slot + recon slots + preset buttons
    // (slot labels now render in the gallery card too)
    expect(
      (await screen.findAllByText("Zip chứa chứng từ lô hàng (txt/pdf/xml)"))
        .length,
    ).toBeGreaterThanOrEqual(1);
    expect(
      screen.getAllByText("Sổ kế toán kỳ này (csv/xlsx export)").length,
    ).toBeGreaterThanOrEqual(1);
    // preset labels now render twice — the intake row and the card's
    // own sample-data buttons
    expect(screen.getAllByText("đối soát")).toHaveLength(2);
    expect(screen.getAllByText("hồ sơ chứng từ")).toHaveLength(2);
  });

  test("an active run narrows slots to its own outcome's spec", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => jsonResponse({})), // registry stays on fallback
    );
    render(<TaskThread creds={creds} run={baseRun} onRun={rs.fn()} />);
    // invoice_recon spec: invoices_zip + books — never dossier_zip
    expect(
      await screen.findByText("Zip chứa file hóa đơn (xml/pdf)"),
    ).toBeTruthy();
    expect(
      screen.getByText("Sổ kế toán kỳ này (csv/xlsx export)"),
    ).toBeTruthy();
    expect(screen.queryByText(/chứng từ lô hàng/)).toBeNull();
  });

  test("pack gallery renders under intake on a fresh request", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => jsonResponse({})), // registry stays on fallback
    );
    render(<TaskThread creds={creds} run={null} onRun={rs.fn()} />);
    // gallery section present; cards from the spec copy (the labels also
    // appear in the router hint, so count the Governed badges)
    expect(
      await screen.findByText("Gói nghiệp vụ governed"),
    ).toBeTruthy();
    expect(
      screen.getAllByText("Governed — niêm phong kiểm chứng"),
    ).toHaveLength(2);
    expect(screen.getAllByText("Sắp có — chưa mở")).toHaveLength(1);
    // the fallback notice stays up while the registry is unread
    expect(screen.getByText(/Spec tĩnh/)).toBeTruthy();
  });

  test("gallery hidden while a run is open; chips fill the intent box", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => jsonResponse({})),
    );
    const { unmount } = render(
      <TaskThread creds={creds} run={baseRun} onRun={rs.fn()} />,
    );
    await screen.findByText("Thiếu đầu vào bắt buộc.");
    expect(screen.queryByText("Gói nghiệp vụ governed")).toBeNull();
    unmount();

    render(<TaskThread creds={creds} run={null} onRun={rs.fn()} />);
    const chip = await screen.findByText("Đối soát hóa đơn kỳ 09/2025");
    fireEvent.click(chip);
    const box = screen.getByPlaceholderText<HTMLTextAreaElement>(
      /Yêu cầu nghiệp vụ/,
    );
    expect(box.value).toBe("Đối soát hóa đơn kỳ 09/2025");
  });

  test("picking a zip slot clears the other zip slot (one zip per request)", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => jsonResponse({})),
    );
    const { container } = render(
      <TaskThread creds={creds} run={null} onRun={rs.fn()} />,
    );
    const inputs = container.querySelectorAll<HTMLInputElement>(
      'input[type="file"]',
    );
    // FALLBACK_SPECS order: dossier_zip, invoices_zip, books
    expect(inputs).toHaveLength(3);
    const dossierInput = inputs[0]!;
    const invoicesInput = inputs[1]!;

    fireEvent.change(dossierInput, {
      target: { files: [new File(["x"], "dossier.zip")] },
    });
    expect(await screen.findByText("dossier.zip")).toBeTruthy();

    fireEvent.change(invoicesInput, {
      target: { files: [new File(["x"], "inv.zip")] },
    });
    expect(await screen.findByText("inv.zip")).toBeTruthy();
    // dossier pick was cleared — its slot shows the placeholder again
    expect(screen.queryByText("dossier.zip")).toBeNull();
  });
});

describe("TaskThread general lane", () => {
  test("a general run renders the lane card and contract_approval question", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => jsonResponse({})),
    );
    const run = {
      workrun_id: "wr-gen",
      tenant_id: "demo",
      outcome_type: "general",
      status: "awaiting_input",
      step_label: "Chờ duyệt tiêu chí",
      intent: "sửa workbook",
      thread_id: "ewcp-wr-gen",
      context: { general: { goal: "sửa workbook" } },
      pending_questions: [
        {
          decision_id: "d-c1",
          kind: "contract_approval",
          prompt: "Agent đề xuất tiêu chí nghiệm thu:\n- c1: file tồn tại",
          options: ["approve_contract", "revise_contract"],
          options_v2: [
            { id: "approve_contract", label: "Duyệt tiêu chí & chạy tiếp" },
            { id: "revise_contract", label: "Yêu cầu sửa tiêu chí" },
          ],
        },
      ],
      deliverables: [],
      ingest_errors: [],
      skipped: [],
    } as RunView;
    render(<TaskThread creds={creds} run={run} onRun={rs.fn()} />);
    expect(await screen.findByText("Lane tổng quát")).toBeTruthy();
    expect(screen.getByText("Duyệt tiêu chí nghiệm thu")).toBeTruthy();
    expect(screen.getByText("Duyệt tiêu chí & chạy tiếp")).toBeTruthy();
  });

  test("deliverable rows expose the deliverable kind chip", async () => {
    rs.stubGlobal(
      "fetch",
      rs.fn(async () => jsonResponse({})),
    );
    const run = {
      workrun_id: "wr-gen",
      tenant_id: "demo",
      outcome_type: "general",
      status: "verified",
      step_label: "x",
      intent: "x",
      pending_questions: [],
      deliverables: [
        {
          deliverable_id: "d1",
          kind: "contract",
          sha256: "abcdef0123456789",
          uri: "outputs/contract.json",
          name: "contract.json",
        },
      ],
      ingest_errors: [],
      skipped: [],
    } as RunView;
    render(<TaskThread creds={creds} run={run} onRun={rs.fn()} />);
    expect(await screen.findByText("contract.json")).toBeTruthy();
    expect(screen.getByText("hợp đồng nghiệm thu")).toBeTruthy();
  });
});
