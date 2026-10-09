"""Deliverable-existence integrity for the general (pane) lane — F2
fabrication guardrail (docs/vnext/F2_FABRICATION_GUARD.md).

The measured failure (GP01_REEVAL task E): a general-lane run ends
`completed` while the deliverables it CLAIMS never existed — `present_files`
normalizes the claim into `values.artifacts` but never stats the file, and
the agent can also name `/mnt/user-data/**` paths in prose. Either way the
self-report is unverifiable until something outside the model checks it.

This module is that check, kept deliberately mechanical:

  1. On the completion seam (RunRecovery._reconcile / RunLauncher.refresh —
     the places the map row is already reconciled against live truth) read
     the run's claimed artifact paths: the `values.artifacts` channel plus
     `/mnt/user-data/**` mentions in assistant messages.
  2. Resolve each claim to a host path via `Paths.resolve_virtual_path`
     (confinement + traversal rules stay upstream-owned) and stat it.
  3. Persist `integrity_flag` on the ExecutionRunMap row:

       * `verified`                          — every claim exists, non-empty
       * `claimed_artifacts_missing:[...]`   — some claim is missing/empty
                                               (JSON list of claim paths)
       * `no_claims`                         — the run claimed nothing
       * NULL                                — never assessed (still
                                               running / state unreadable)

  The flag is advisory — a flagged run still reports `completed`; the map
  view surfaces it so the UI can render "unverified self-report" instead of
  a clean finish. It is durable once written: claims are completion-time
  truth, so a later reconcile neither recomputes nor clears it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .execution_run_store import ExecutionRunRecord, ExecutionRunStore

if TYPE_CHECKING:
    from deerflow.config.paths import Paths

logger = logging.getLogger(__name__)

__all__ = [
    "FLAG_CLAIMED_MISSING_PREFIX",
    "INTEGRITY_NO_CLAIMS",
    "INTEGRITY_VERIFIED",
    "ClaimedArtifactProbe",
    "HostOutputsProbe",
    "assess_deliverable_integrity",
    "extract_claimed_artifacts",
    "flag_missing_claims",
    "parse_missing_claims",
]

#: Flag vocabulary — persisted verbatim on the map row; the UI keys on the
#: `claimed_artifacts_missing:` prefix, everything else renders as clean.
FLAG_CLAIMED_MISSING_PREFIX = "claimed_artifacts_missing"
INTEGRITY_VERIFIED = "verified"
INTEGRITY_NO_CLAIMS = "no_claims"

_VIRTUAL_PREFIX = "/mnt/user-data"
# `/mnt/user-data/**` mentions in assistant prose — the second claim channel
# when a model names a deliverable without going through `present_files`.
_CLAIM_RE = re.compile(r"/mnt/user-data/[^\s\"'`()\[\]{}<>|]+")
# Trailing sentence punctuation a prose claim legitimately drags along.
_CLAIM_TRAILERS = ".,;:!?*~"

_AI_TYPES = {"ai", "assistant", "AIMessage", "AIMessageChunk"}


def flag_missing_claims(names: Sequence[str]) -> str:
    return f"{FLAG_CLAIMED_MISSING_PREFIX}:{json.dumps(sorted(names), ensure_ascii=False)}"


def parse_missing_claims(flag: str | None) -> list[str] | None:
    """Decode a `claimed_artifacts_missing:` flag back to its names; None
    for any other flag value (verified/no_claims/unset)."""
    if flag is None or not flag.startswith(FLAG_CLAIMED_MISSING_PREFIX + ":"):
        return None
    try:
        parsed = json.loads(flag.split(":", 1)[1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        parts = []
        for part in content:
            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(parts)
    return ""


def _message_role_and_text(message: Any) -> tuple[str | None, str]:
    """Normalize dict- and object-shaped thread messages to (type, text).

    `get_state` returns `values.messages` as LangChain-serialized entries;
    their shape varies by serializer (typed dicts, `kwargs` constructor
    dicts, live BaseMessage objects in embedded runs), so read defensively.
    """
    if isinstance(message, Mapping):
        role = message.get("type") or message.get("role")
        if role is None and message.get("type") == "constructor":
            ids = message.get("id") or []
            role = str(ids[-1]) if ids else None
        content = message.get("content")
        if content is None and isinstance(message.get("kwargs"), Mapping):
            content = message["kwargs"].get("content")
        return (str(role) if role is not None else None, _message_text(content))
    role = getattr(message, "type", None) or getattr(message, "role", None)
    return (str(role) if role is not None else None, _message_text(getattr(message, "content", None)))


def _is_ai_message(role: str | None) -> bool:
    if role is None:
        return False
    if role in _AI_TYPES:
        return True
    # Serialized-constructor ids come through as the leaf class name.
    return role.endswith("AIMessage") or role.endswith("AIMessageChunk")


def _claim_name(virtual_path: str) -> str:
    """The flag/display form of a claim: path relative to `/mnt/user-data/`."""
    stripped = virtual_path.lstrip("/")
    prefix = _VIRTUAL_PREFIX.lstrip("/")
    if stripped == prefix:
        return ""
    return stripped[len(prefix) :].lstrip("/")


def extract_claimed_artifacts(thread_state: Mapping[str, Any] | None) -> list[str]:
    """The deliverable paths a finished run CLAIMS, in claim order, deduped.

    Channels (union):
      * `values.artifacts` — the `present_files` tool's normalized claims
        (already `/mnt/user-data/**` by tool confinement);
      * `/mnt/user-data/**` path mentions inside assistant messages — the
        prose claim a model can make without ever calling present_files.

    Anything outside the sandbox virtual root is not a deliverable claim.
    """
    if not isinstance(thread_state, Mapping):
        return []
    values = thread_state.get("values")
    if not isinstance(values, Mapping):
        return []
    claims: list[str] = []

    def collect(raw: Any) -> None:
        if not isinstance(raw, str):
            return
        for match in _CLAIM_RE.finditer(raw):
            claim = match.group(0).rstrip(_CLAIM_TRAILERS)
            if claim not in claims:
                claims.append(claim)

    artifacts = values.get("artifacts")
    if isinstance(artifacts, Sequence) and not isinstance(artifacts, (str, bytes, bytearray)):
        for entry in artifacts:
            collect(entry if isinstance(entry, str) else "")

    messages = values.get("messages")
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes, bytearray)):
        for message in messages:
            role, text = _message_role_and_text(message)
            if _is_ai_message(role):
                collect(text)

    return claims


@runtime_checkable
class ClaimedArtifactProbe(Protocol):
    """Existence oracle for claimed deliverable paths — injectable so tests
    (and a future in-sandbox listing probe) can swap the host-stat impl."""

    async def missing(self, *, thread_id: str, user_id: str, claims: Sequence[str]) -> list[str]:
        """Return the claims (in `_claim_name` form) that do not resolve to a
        non-empty host file. Anything unresolvable counts as missing."""
        ...


class HostOutputsProbe:
    """Stat claims against the host sandbox dirs.

    The pane lane's sandbox outputs land under
    `{base}/users/{safe_uid}/threads/{tid}/user-data/`; pre-user-scoping
    threads live at `{base}/threads/{tid}/user-data/`. A claim verifies when
    EITHER layout resolves to a non-empty file — the probe is read-only and
    never touches sandbox state.
    """

    def __init__(self, *, paths_getter: Callable[[], Paths] | None = None) -> None:
        if paths_getter is None:
            from deerflow.config.paths import get_paths

            paths_getter = get_paths
        self._paths_getter = paths_getter

    async def missing(self, *, thread_id: str, user_id: str, claims: Sequence[str]) -> list[str]:
        return await asyncio.to_thread(self._missing_sync, thread_id=thread_id, user_id=user_id, claims=list(claims))

    def _missing_sync(self, *, thread_id: str, user_id: str, claims: list[str]) -> list[str]:
        from deerflow.config.paths import make_safe_user_id

        paths = self._paths_getter()
        safe_uid = make_safe_user_id(user_id)
        missing: list[str] = []
        for claim in claims:
            if not self._exists(paths, thread_id, safe_uid, claim):
                missing.append(_claim_name(claim) or claim)
        return missing

    @staticmethod
    def _exists(paths: Paths, thread_id: str, safe_uid: str, claim: str) -> bool:
        for uid in (safe_uid, None):  # user-scoped first, legacy layout second
            try:
                resolved = paths.resolve_virtual_path(thread_id, claim, user_id=uid)
            except (ValueError, OSError):
                continue
            try:
                if resolved.is_file() and resolved.stat().st_size > 0:
                    return True
            except OSError:
                continue
        return False


async def assess_deliverable_integrity(
    *,
    store: ExecutionRunStore,
    record: ExecutionRunRecord,
    state: Mapping[str, Any] | None,
    probe: ClaimedArtifactProbe | None = None,
) -> str | None:
    """Assess one map row's claimed deliverables and persist the flag once.

    Gates — returns the stored flag (or None when it does not apply):
      * `task_mode` must be `general` — governed rows are covered by the
        kernel manifest/seal verify path, invoke rows are capability calls;
      * `status` must be `completed` — claims on an in-flight or
        failed/interrupted run are not a finished self-report;
      * `integrity_flag` still NULL — the assessment is durable, claims are
        completion-time truth.

    Probe or get_state failures leave the flag NULL and are logged, never
    raised: integrity assessment must not break run reconciliation.
    """
    if record.integrity_flag is not None:
        return record.integrity_flag
    if record.task_mode != "general" or record.status != "completed":
        return None

    claims = extract_claimed_artifacts(state)
    if not claims:
        flag = INTEGRITY_NO_CLAIMS
    else:
        probe = probe or HostOutputsProbe()
        try:
            missing = await probe.missing(thread_id=record.thread_id, user_id=record.created_by, claims=claims)
        except Exception:
            logger.warning(
                "ewcp: deliverable-integrity probe failed for %s — leaving flag unset",
                record.execution_run_id,
                exc_info=True,
            )
            return None
        flag = flag_missing_claims(missing) if missing else INTEGRITY_VERIFIED

    try:
        await store.update_integrity_flag(record.execution_run_id, flag)
    except Exception:
        logger.warning(
            "ewcp: could not persist integrity_flag for %s",
            record.execution_run_id,
            exc_info=True,
        )
        return None
    return flag
