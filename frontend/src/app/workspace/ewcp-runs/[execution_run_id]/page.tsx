// Deep link into the ExecutionRun surface: /workspace/ewcp-runs/{id}
// preselects the run (A6 proposal #4 — a governed run must be
// linkable from a chat thread badge or an external reference).

import { EwcpRunsPage } from "@/ewcp/components/ewcp-runs-page";

export default async function Page({
  params,
}: {
  params: Promise<{ execution_run_id: string }>;
}) {
  const { execution_run_id } = await params;
  return <EwcpRunsPage initialActiveId={execution_run_id} />;
}
