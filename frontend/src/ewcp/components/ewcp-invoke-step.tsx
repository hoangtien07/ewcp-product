"use client";

// ewcp_invoke governed-result step — WP-A6 item #13 (hybrid lane): when
// the agent invokes a governed capability mid-chat, the tool-call step
// renders the governed result — capability id, kernel run state via
// lifecycleStatus, and the ExecutionRunMap deep link — instead of the
// raw JSON the generic tool transcript would show. Non-invoke tools and
// the upstream message path are untouched: this component is reached
// only through the `ewcp_invoke` branch of getToolCallKind.

import type { Message } from "@langchain/langgraph-sdk";
import { ShieldCheckIcon } from "lucide-react";
import Link from "next/link";

import { ChainOfThoughtStep } from "@/components/ai-elements/chain-of-thought";
import { extractTextFromMessage } from "@/core/messages/utils";

import { lifecycleStatus, OUTCOME_LABEL } from "../labels";

import { LifecycleBadge } from "./lifecycle-badge";

interface InvokeRunView {
  status?: string;
  workrun_id?: string;
  outcome_type?: string;
  decision?: { answer: string; decided_by: string };
}

interface InvokeResultPayload {
  ok?: boolean;
  error?: string;
  message?: string;
  hint?: string;
  capability_id?: string;
  invocation_id?: string;
  execution_run_id?: string;
  execution_run_map_id?: string | null;
  idempotent_replay?: boolean;
  run?: InvokeRunView;
}

function parseInvokePayload(
  resultMessage: Extract<Message, { type: "tool" }>,
): InvokeResultPayload | null {
  const text = extractTextFromMessage(resultMessage);
  if (!text) {
    return null;
  }
  try {
    const parsed: unknown = JSON.parse(text);
    return parsed && typeof parsed === "object"
      ? (parsed as InvokeResultPayload)
      : null;
  } catch {
    return null;
  }
}

export function EwcpInvokeStep({
  args,
  resultMessage,
}: {
  args: Record<string, unknown>;
  resultMessage?: Extract<Message, { type: "tool" }>;
}) {
  const outcomeType =
    typeof args.outcome_type === "string" ? args.outcome_type : undefined;
  const label = `EWCP · ${
    outcomeType ? (OUTCOME_LABEL[outcomeType] ?? outcomeType) : "invoke"
  }`;
  return (
    <ChainOfThoughtStep label={label} icon={ShieldCheckIcon}>
      {resultMessage && <EwcpInvokeResultCard resultMessage={resultMessage} />}
    </ChainOfThoughtStep>
  );
}

function EwcpInvokeResultCard({
  resultMessage,
}: {
  resultMessage: Extract<Message, { type: "tool" }>;
}) {
  const payload = parseInvokePayload(resultMessage);
  if (payload === null) {
    // Not the tool's JSON contract — surface the raw result rather than
    // hide it, same spirit as the generic details disclosure.
    return (
      <pre className="bg-muted max-h-64 overflow-auto rounded p-2 text-xs break-all whitespace-pre-wrap">
        {extractTextFromMessage(resultMessage)}
      </pre>
    );
  }
  if (payload.ok !== true) {
    return (
      <div className="border-destructive/40 space-y-1 rounded-md border p-3">
        <div className="text-destructive text-xs font-medium">
          {payload.error ?? "invoke_failed"}
        </div>
        {payload.message && <p className="text-xs">{payload.message}</p>}
        {payload.hint && (
          <p className="text-muted-foreground text-xs">{payload.hint}</p>
        )}
      </div>
    );
  }
  const run = payload.run ?? {};
  const lifecycle = lifecycleStatus(
    { status: "running" },
    { status: run.status ?? "", decision: run.decision },
  );
  return (
    <div className="border-border bg-card space-y-2 rounded-md border p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">
          {payload.capability_id ??
            (run.outcome_type
              ? (OUTCOME_LABEL[run.outcome_type] ?? run.outcome_type)
              : "ewcp_invoke")}
        </span>
        <LifecycleBadge status={lifecycle} />
      </div>
      <div className="text-muted-foreground flex flex-wrap items-center gap-2 font-mono text-[10px]">
        {run.workrun_id && (
          <span title="workrun_id">wr:{run.workrun_id.slice(0, 8)}…</span>
        )}
        {payload.invocation_id && (
          <span title="invocation_id">
            inv:{payload.invocation_id.slice(0, 12)}…
          </span>
        )}
        {payload.idempotent_replay === true && (
          <span title="idempotent replay">replay</span>
        )}
      </div>
      {payload.execution_run_map_id && (
        <Link
          href={`/workspace/ewcp-runs/${encodeURIComponent(payload.execution_run_map_id)}`}
          className="text-primary inline-block text-xs underline-offset-2 hover:underline"
        >
          Xem run được quản trị →
        </Link>
      )}
    </div>
  );
}
