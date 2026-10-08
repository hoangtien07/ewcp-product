// Exploratory bridge — "khám phá" intents launch an ExecutionRun on the
// product's general lane (AgentRuns) instead of the kernel: same intake
// box, real threads + SSE observation. The composer-draft handoff from
// the legacy pane is gone — the launched run carries the intent itself.

import { launchExecutionRun, type ExecutionRun } from "@/ewcp/api";

/** Launch a general-lane ExecutionRun for a free-form intent. Returns
 * the map record — callers navigate to its detail/surface. */
export async function handoffToGeneralLane(
  intent: string,
  opts?: { files?: File[]; idempotencyKey?: string },
): Promise<ExecutionRun> {
  const { run } = await launchExecutionRun({
    intent,
    taskMode: "general",
    files: opts?.files,
    idempotencyKey: opts?.idempotencyKey,
  });
  return run;
}
