"use client";

// ExecutionRun surface — list + intent intake + detail on the SSE
// join route. Rehomed into the workspace views under /workspace/
// (A3 Task 7); sidebar entry "Công việc" in workspace-nav-chat-list
// (A6-11 naming: workspace = "Công việc", verification = "Kiểm chứng").
// The body lives in ewcp-runs-page so /workspace/ewcp-runs/{id}
// deep links into the same view (A6 proposal #4).

import { EwcpRunsPage } from "@/ewcp/components/ewcp-runs-page";

export default function Page() {
  return <EwcpRunsPage />;
}
