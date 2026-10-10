# N04 — SSE Recovery Honesty (diagnosis + one bounded repair)

Date: 2026-10-10. Worker W4, branch `product/vnext` @ `e0ac3aea`.
Question (from the overnight spec): when the SSE stream is the deliverable
surface, are there cases where the pane claims completion the kernel never
produced, or a dropped/replayed stream silently changes what the user sees?

Method: code-level trace of the whole path — `GET …/runs/{rid}/join`
(`backend/app/gateway/routers/thread_runs.py:1312`) → `sse_consumer`
(`backend/app/gateway/services.py:2388`) → `MemoryStreamBridge`
(`backend/packages/harness/deerflow/runtime/stream_bridge/memory.py`) →
nginx `/api/langgraph` rewrite → pane `joinRunStream`
(`frontend/src/ewcp/api.ts:466`) → `ExecutionRunThread`
(`frontend/src/ewcp/components/execution-run-thread.tsx`). Behavior pinned
by unit/DOM tests, not assumed. Prior measured matrices:
[A7A_SSE_RECONNECT.md](A7A_SSE_RECONNECT.md) (protocol 9/9),
[A7A_RELIABILITY.md](A7A_RELIABILITY.md) (browser recovery).

## Verdicts

| # | Scenario | Verdict | Evidence |
|---|----------|---------|----------|
| a | Stream ends mid-run with NO terminal event | **DISHONEST → FIXED** | below |
| b | Replay after `stream_replay_gap` | **honest at protocol; pane-unreachable; remount truncation silent (documented)** | below |
| c | Kernel workrun status vs last SSE event | **honest** | below |
| d | Stale / forged `Last-Event-ID` | **honest except one pathological edge — pinned, declined (upstream)** | below |

## (a) Mid-run stream end with no terminal frame — DISHONEST, fixed

**Server side is honest where it can be.** Join on a terminal record whose
stream is gone emits a bare `end` (`services.py:2419-2436`); join on a
`store_only` live record a memory bridge can't serve → `409` "not active on
this worker" (`thread_runs.py:1322-1323`); orphaned runs are reconciled to
`error` at startup (`runs/manager.py:1892+`, `ORPHAN_RECOVERY_STOP_REASON`),
so post-restart joins converge to one of those honest outcomes.

**The pane side froze silently.** `joinRunStream` resolves when the body
EOF's or rejects on transport failure — both indistinguishable from a clean
end to the component, which swallowed all errors and ran one
`getExecutionRun(refresh=1)` in `.finally`
(`execution-run-thread.tsx`, SSE effect). When the refreshed record was
still `running`/`launching`/`pending_interrupt`, the effect did not rejoin
(deps are `run?.join_url, run?.run_id` — unchanged by a status-only
refresh), no polling exists, and nothing marked the feed as dead. The pane
then displayed the last-known status (`Đã tiếp nhận`) as a settled display:
literally the last durable truth, but presented as current while every
later kernel/run transition (including `candidate_complete` + the approval
gate) stayed invisible until remount or manual "Làm mới". This is the
"silently frozen" case the spec asked about — the only difference from a
clean end was the missing `· trực tiếp` dot.

Latent server-side half (pinned, not reached in the pane): a join on a
live record with **no** stream does not error — `subscribe()` creates a
fresh empty stream and the consumer sits on heartbeats forever
(`memory.py:133`, `services.py:2462-2467`). Pinned by
`test_join_on_live_run_with_no_stream_hangs_on_heartbeats` — no
"nothing-left-to-send" signal exists at protocol level for a live record.

**Repair (extension-owned, `frontend/src/ewcp` only):** track whether a
terminal frame (`end`/`error`) was seen; when the stream ends without one
and the post-refresh record is still live — or the refresh itself fails —
render `Mất kết nối trực tiếp` so the stale snapshot is marked rather than
presented as current. Aborted/unmounted streams excluded. ~20 LOC, no
upstream touch. Deliberately NOT an auto-rejoin (cursor-less rejoin gets
the retained tail or nothing; reconnect UX is the bigger item, recorded
below).

## (b) Replay gap visibility — honest at protocol; pane can't reach it

- `sse_consumer` emits `event: gap` + `{"code":"stream_replay_gap",…,
  "recovery":"reload_durable_state"}` then closes (`services.py:2447-2460`)
  — an explicit boundary frame, never blended into data events. A
  create-endpoint retry whose terminal stream is gone gets the same frame
  via `emit_gap_on_missing_stream` (`services.py:2419-2434`).
- `joinRunStream` passes it through verbatim to `onEvent`
  (`api.ts:505-511`) — pinned by `api.test.ts` "surfaces a server gap
  frame verbatim" — and the feed renders it as a visible `gap`/`stream_replay_gap`
  line, not an ordinary event — pinned by the DOM test "a server gap
  frame surfaces in the activity feed".
- **But the pane never sends `Last-Event-ID`** (`api.ts:473-476` sends
  only `Accept`), so the gap path is unreachable from the pane today
  (documented in A7A_SSE_RECONNECT §Limits). The pane's own recovery case
  is a remount tail-only replay — events evicted below the watermark are
  simply absent and the retained tail renders contiguously with no
  truncation marker (pinned: "tail-only replay renders contiguously…").
  Recorded upstream as cosmetic-by-design: durable truth never rides the
  feed.

Verdict: **honest** where the contract is reachable; the silent pane
truncation is a documented cosmetic limit, not a claim of completeness.

## (c) Kernel workrun status vs last SSE event — honest

Completion claims derive only from durable records: the badge is
`lifecycleStatus(run, workrun)` (`labels.ts:162-204`), and a bound
workrun's kernel truth **wins over** the launcher record — the stream
never decides. Pinned:

- `run.status=completed` + `workrun.status=running` → `accepted`/`Đã tiếp nhận`
  (kernel still working → no completion claim).
- `run.status=completed` + `workrun.status=failed` → `UNKNOWN`/`Không rõ`
  (not `agent_finished`).
- `run.status=running` + `workrun.status=candidate_complete` → `agent_finished`
  — the kernel-side gate surfaces even when the product record lags.

Residual: inside the frozen-window of (a) the last-known record could be
shown as current — covered by the (a) marker. Verdict: **honest**.

## (d) Stale / forged `Last-Event-ID` — honest except one pathological edge

`_resolve_start_offset` (`memory.py:75-100`) behavior, pinned:

- Well-formed cursor **below** the retained watermark (`seq < start_offset`)
  → `StreamGap` → `event: gap` + `recovery: reload_durable_state` — the
  honest signal; already pinned upstream
  (`test_evicted_last_event_id_yields_gap…`, foreign low cursor likewise).
- Malformed / foreign / **future-seq** cursor → replay-from-earliest —
  over-delivery, not loss (pinned `test_future_seq_cursor_falls_back_to_full_replay`).
- **Pinned edge — silent under-delivery:** ids are `{ms}-{seq}` — `seq` a
  per-run offset, `ms` wall-clock. A foreign run's id that coincidentally
  matches a retained id **is accepted** as the resume point; earlier
  retained events are skipped for that client
  (`test_cross_run_id_collision_is_accepted_as_resume_point`). Fixing needs
  run-namespaced event ids — upstream bridge code — declined here and
  recorded; the collision needs two runs publishing the same seq inside
  the same millisecond.

Verdict: **honest** for every realistic cursor; one pathological
under-delivery edge pinned, declined (upstream-owned).

## Declined (recorded, out of the one-repair budget)

- **Auto-rejoin with backoff on drop** — real fix for the frozen window;
  needs cursor-less rejoin semantics decided (tail replay vs durable
  refresh) and retry UX. Bigger than one repair.
- **Pane `Last-Event-ID` + gap UX** — makes the server contract reachable;
  requires durable last-event-id tracking in the pane and a gap-recovery
  surface. Larger item.
- **Run-namespaced event ids** — upstream bridge change for the (d)
  collision edge.
- **"Nothing-left-to-send" signal for live records** — the heartbeat-forever
  join has no protocol marker; upstream `sse_consumer` territory.

## Tests

- `backend/extensions/ewcp-core/tests/test_sse_recovery_honesty.py` — 3 new:
  future-seq cursor → full replay; cross-run id collision → accepted,
  silent skip (pinned); live-record/no-stream join → heartbeat loop
  (no error, no end).
- `frontend/tests/unit/ewcp/api.test.ts` — 1 new: `gap` frame reaches
  `onEvent` verbatim.
- `frontend/tests/unit/ewcp/execution-run-thread.dom.test.tsx` — 7 new:
  drop keeps last-known status (pinned); **red→green** marker test for the
  (a) fix; clean `end` does not claim a drop; `gap` surfaces in the feed;
  tail-only replay contiguous, no truncation marker (pinned silent);
  kernel-running and kernel-failed divergence pins.

Total: 11 new assertions across 3 files, all green post-fix
(`178` frontend DOM+api tests incl. existing, `3` backend).

## SHA trail

- `0b68db3f` — test: N04 honesty pins (red test first, per protocol).
- `69511eac` — fix: stream-loss marker in `ExecutionRunThread`.
