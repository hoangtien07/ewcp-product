// Exploratory-lane handoff — when the governed lane dead-ends on a clarify
// response, re-intake the intent into the general assistant lane (the
// upstream lead_agent chat). Q24 firewall (docs/ewcp/PA_A_LAYOUT.md):
// re-intake only, never a promote — the handed-off thread is an ordinary
// exploratory artifact and carries UNVERIFIED chrome on this pane.
//
// Wiring reuses upstream mechanisms verbatim: POST /api/threads materializes
// the lane-side thread up front (idempotent, same as the chat page's
// project pre-create), and the composer draft in sessionStorage prefills
// the intent so the user still performs the send — the pane never runs an
// agent itself.

import { createThread } from "@/core/threads/api";
import {
  buildComposerDraftKey,
  getSessionComposerDraftStorage,
  writeComposerDraft,
  type ComposerDraftStorage,
} from "@/core/threads/composer-draft";

export interface ExploratoryHandoff {
  thread_id: string;
  intent: string;
  at: string; // ISO-8601
}

const HANDOFFS_KEY = "ewcp_exploratory_handoffs";
const HANDOFFS_CAP = 20;

// Handoff records carry only non-secret ids (thread_id + the intent text
// itself) — the no-tokens-in-web-storage rule holds.
export function readHandoffs(
  storage: Pick<Storage, "getItem"> | null,
): ExploratoryHandoff[] {
  try {
    const raw = storage?.getItem(HANDOFFS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (h): h is ExploratoryHandoff =>
        typeof h === "object" &&
        h !== null &&
        typeof (h as ExploratoryHandoff).thread_id === "string" &&
        typeof (h as ExploratoryHandoff).intent === "string" &&
        typeof (h as ExploratoryHandoff).at === "string",
    );
  } catch {
    return [];
  }
}

export function recordHandoff(
  handoff: ExploratoryHandoff,
  storage: Pick<Storage, "getItem" | "setItem"> | null,
): ExploratoryHandoff[] {
  const next = [
    handoff,
    ...readHandoffs(storage).filter((h) => h.thread_id !== handoff.thread_id),
  ].slice(0, HANDOFFS_CAP);
  try {
    storage?.setItem(HANDOFFS_KEY, JSON.stringify(next));
  } catch {
    /* private mode — the in-memory copy is still returned */
  }
  return next;
}

export function handoffThreadPath(threadId: string): string {
  return `/workspace/chats/${encodeURIComponent(threadId)}`;
}

// The chat composer keys its draft by the same user id AuthProvider exposes
// (synthetic "default" in auth-disabled dev, the real id otherwise,
// "anonymous" when /me is unavailable) — resolve it identically so the
// prefilled draft lands on the key the chat page actually reads.
async function resolveComposerUserId(): Promise<string> {
  try {
    const res = await fetch("/api/v1/auth/me", { credentials: "include" });
    if (res.ok) {
      const data = (await res.json()) as { id?: unknown };
      if (typeof data.id === "string" && data.id) return data.id;
    }
  } catch {
    /* fall through to anonymous */
  }
  return "anonymous";
}

export async function handoffToGeneralLane({
  intent,
  clarifyQuestion,
  storage,
}: {
  intent: string;
  clarifyQuestion?: string | null;
  storage?: ComposerDraftStorage | null;
}): Promise<ExploratoryHandoff> {
  const threadId = crypto.randomUUID();
  // Materialize the thread first: the pane needs a real exploratory artifact
  // to badge + link, and the idempotent create is a no-op if the user's
  // first send would have created the row anyway.
  await createThread(threadId);

  const text = clarifyQuestion
    ? `${intent}\n\n(Chuyển từ lane governed — hệ thống cần rõ hơn: ${clarifyQuestion})`
    : intent;
  const draftStorage = storage ?? getSessionComposerDraftStorage();
  writeComposerDraft(
    draftStorage,
    buildComposerDraftKey({
      userId: await resolveComposerUserId(),
      agentName: null,
      threadId,
    }),
    { text, skillName: null },
  );

  const handoff: ExploratoryHandoff = {
    thread_id: threadId,
    intent,
    at: new Date().toISOString(),
  };
  recordHandoff(handoff, draftStorage);
  return handoff;
}
