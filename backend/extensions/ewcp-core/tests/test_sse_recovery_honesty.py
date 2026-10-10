"""N04 — SSE recovery honesty pins at the stream-bridge level.

The EWCP pane observes runs through ``GET /api/threads/{tid}/runs/{rid}/join``
(``sse_consumer`` in ``app.gateway.services``), which serializes whatever
``MemoryStreamBridge.subscribe`` yields for the caller's ``Last-Event-ID``.
These tests pin the TRUE current behavior for forged, stale, and
cross-run cursors — verdicts are written up in
``docs/vnext/SSE_RECOVERY_HONESTY.md``. The pane itself never sends a
cursor; these pins cover the contract a resuming consumer would hit.
"""

import pytest

from deerflow.runtime import (
    END_SENTINEL,
    HEARTBEAT_SENTINEL,
    MemoryStreamBridge,
    StreamEvent,
    StreamGap,
)


@pytest.mark.asyncio
async def test_future_seq_cursor_falls_back_to_full_replay() -> None:
    """(d) A forged cursor *ahead* of the retained window cannot skip
    events: the seq is verifiable only inside the window, so an unknown
    forward id degrades to replay-from-earliest (over-delivery — honest,
    the client loses nothing retained)."""
    bridge = MemoryStreamBridge(queue_maxsize=4)
    run_id = "run-future-cursor"
    for index in range(3):
        await bridge.publish(run_id, f"e{index}", {"index": index})
    await bridge.publish_end(run_id)

    stream = bridge._streams[run_id]
    ts, _, _ = stream.events[-1].id.rpartition("-")
    forged = f"{ts}-99"  # well-formed, same ms bucket, seq beyond latest

    received = [
        entry
        async for entry in bridge.subscribe(
            run_id, last_event_id=forged, heartbeat_interval=1.0
        )
    ]

    assert [entry.event for entry in received[:-1]] == ["e0", "e1", "e2"]
    assert received[-1] is END_SENTINEL
    assert all(not isinstance(entry, StreamGap) for entry in received)


@pytest.mark.asyncio
async def test_cross_run_id_collision_is_accepted_as_resume_point() -> None:
    """(d) Pinned edge: event ids are ``{ms}-{seq}`` — ``seq`` is a per-run
    offset and ``ms`` is wall-clock, so an id minted by ANOTHER run that
    coincidentally matches a retained id IS accepted as this run's resume
    point. Events before that offset are then silently skipped for the
    resuming client — the one under-delivery case in the cursor contract.
    Pinning it (not fixing): collision-proofing needs run-namespaced ids,
    which is upstream bridge code, not extension surface."""
    bridge = MemoryStreamBridge(queue_maxsize=8)
    run_id = "run-b"
    stream = bridge._get_or_create_stream(run_id)
    # Hand-build a retained buffer identical to what publish() leaves:
    for index in range(4):
        stream.events.append(
            StreamEvent(id=f"1000-{index}", event=f"e{index}", data={})
        )
    stream.start_offset = 0

    # "1000-2" could have been minted by a different run publishing in the
    # same millisecond — the retained-id check cannot tell foreign from own.
    received: list[StreamEvent] = []
    async for entry in bridge.subscribe(
        run_id, last_event_id="1000-2", heartbeat_interval=0.05
    ):
        if entry is HEARTBEAT_SENTINEL:
            break  # reached the live tail — stop measuring
        received.append(entry)

    # The cursor verifies at local index 2, so replay resumes at offset 3 —
    # e0..e2 are silently skipped for this client.
    assert [entry.event for entry in received] == ["e3"]


@pytest.mark.asyncio
async def test_join_on_live_run_with_no_stream_hangs_on_heartbeats() -> None:
    """(a) Server-side half of the mid-run-drop scenario: a join on a
    ``running`` record whose stream is missing does NOT error — subscribe
    transparently creates a fresh empty stream and the consumer sits on
    heartbeats until the run (if ever) publishes again. The SSE layer has
    no "nothing left to send" signal for a live record, so a client cannot
    distinguish "quiet run" from "dead stream" at protocol level."""
    bridge = MemoryStreamBridge(queue_maxsize=4)
    run_id = "run-live-no-stream"

    received: list[object] = []
    async for entry in bridge.subscribe(run_id, heartbeat_interval=0.01):
        received.append(entry)
        if len(received) >= 3:
            break

    assert received == [
        HEARTBEAT_SENTINEL,
        HEARTBEAT_SENTINEL,
        HEARTBEAT_SENTINEL,
    ]
    # The stream object now exists with zero events — the join manufactured
    # it rather than reporting the run as unstreamable.
    assert bridge._streams[run_id].events == []
