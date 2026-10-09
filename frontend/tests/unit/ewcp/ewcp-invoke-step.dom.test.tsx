import type { Message } from "@langchain/langgraph-sdk";
import { afterEach, describe, expect, it, rs } from "@rstest/core";
import { cleanup, render, screen } from "@testing-library/react";

import { MessageGroup } from "@/components/workspace/messages/message-group";
import { I18nContext } from "@/core/i18n/context";
import { enUS } from "@/core/i18n/locales/en-US";
import { EwcpInvokeStep } from "@/ewcp/components/ewcp-invoke-step";

rs.mock("@/components/workspace/artifacts", () => ({
  useArtifacts: () => ({
    artifacts: [],
    setArtifacts: () => undefined,
    selectedArtifact: null,
    autoSelect: false,
    select: () => undefined,
    deselect: () => undefined,
    open: false,
    autoOpen: false,
    setOpen: () => undefined,
  }),
}));

afterEach(cleanup);

const OK_PAYLOAD = {
  ok: true,
  capability_id: "cap:invoice_recon",
  invocation_id: "invo-1234567890ab",
  execution_run_id: "raw-run-id",
  execution_run_map_id: "er_abc123",
  idempotent_replay: false,
  run: {
    workrun_id: "wr-invoice-77",
    status: "verified",
    outcome_type: "invoice_recon",
  },
};

function toolMessage(content: string, name = "ewcp_invoke") {
  return {
    id: "tool-1",
    type: "tool",
    name,
    tool_call_id: "call-1",
    content,
  } as Extract<Message, { type: "tool" }>;
}

function invokeGroupMessages(payloadContent: string): Message[] {
  return [
    {
      id: "ai-1",
      type: "ai",
      content: "",
      tool_calls: [
        {
          id: "call-1",
          name: "ewcp_invoke",
          args: { outcome_type: "invoice_recon", context: { period: "p" } },
        },
      ],
    } as Message,
    toolMessage(payloadContent),
  ];
}

function renderGroup(messages: Message[]) {
  return render(
    <I18nContext.Provider
      value={{ locale: "en-US", setLocale: () => undefined, t: enUS }}
    >
      <MessageGroup messages={messages} />
    </I18nContext.Provider>,
  );
}

describe("EwcpInvokeStep (A6 #13 hybrid lane)", () => {
  it("renders the governed card: capability, kernel lifecycle, run deep link", () => {
    render(
      <EwcpInvokeStep
        args={{ outcome_type: "invoice_recon" }}
        resultMessage={toolMessage(JSON.stringify(OK_PAYLOAD))}
      />,
    );
    // Outcome label + capability id — not raw JSON
    expect(screen.getByText(/Đối soát hóa đơn/)).toBeTruthy();
    expect(screen.getByText("cap:invoice_recon")).toBeTruthy();
    // kernel workrun status wins the lifecycle vocabulary
    expect(screen.getByText("Đã niêm phong")).toBeTruthy();
    // deep link routes to the ExecutionRunMap row, not the raw run id
    const link = screen.getByRole("link", { name: /Xem run được quản trị/ });
    expect(link.getAttribute("href")).toBe("/workspace/ewcp-runs/er_abc123");
    expect(document.body.innerHTML).not.toContain('"ok":true');
  });

  it("renders the tool's structured error without a lifecycle badge", () => {
    const err = JSON.stringify({
      ok: false,
      error: "schema_invalid",
      message: "input missing required field",
      hint: "supply the fields listed in `missing`",
    });
    const { container } = render(
      <EwcpInvokeStep
        args={{ outcome_type: "invoice_recon" }}
        resultMessage={toolMessage(err)}
      />,
    );
    expect(container.textContent).toContain("schema_invalid");
    expect(container.textContent).toContain("input missing required field");
    expect(container.textContent).toContain("supply the fields listed");
    expect(container.textContent).not.toContain("Xem run được quản trị");
  });

  it("falls back to raw content when the result is not the invoke payload", () => {
    render(
      <EwcpInvokeStep
        args={{ outcome_type: "invoice_recon" }}
        resultMessage={toolMessage("plain tool output")}
      />,
    );
    expect(screen.getByText("plain tool output")).toBeTruthy();
  });

  it("renders just the step label while the call is streaming", () => {
    const { container } = render(
      <EwcpInvokeStep args={{ outcome_type: "invoice_recon" }} />,
    );
    expect(container.textContent).toContain("Kiểm chứng · Đối soát hóa đơn");
    expect(container.querySelector("pre")).toBeNull();
  });
});

describe("MessageGroup tool-render seam", () => {
  it("routes ewcp_invoke calls to the governed card inside the message list", () => {
    const { container } = renderGroup(
      invokeGroupMessages(JSON.stringify(OK_PAYLOAD)),
    );
    expect(container.textContent).toContain("Kiểm chứng · Đối soát hóa đơn");
    expect(container.textContent).toContain("cap:invoice_recon");
    const link = container.querySelector(
      "a[href='/workspace/ewcp-runs/er_abc123']",
    );
    expect(link).toBeTruthy();
  });

  it("leaves non-ewcp tools on the untouched generic renderer", () => {
    const { container } = renderGroup([
      {
        id: "ai-1",
        type: "ai",
        content: "",
        tool_calls: [{ id: "call-1", name: "mcp_custom", args: { x: 1 } }],
      } as Message,
      toolMessage('{"value":1}', "mcp_custom"),
    ]);
    // generic label + details disclosure, no ewcp card markup
    expect(container.textContent).toContain('Use "mcp_custom" tool');
    expect(container.textContent).not.toContain("Kiểm chứng ·");
    expect(container.textContent).not.toContain("Đã niêm phong");
  });
});
