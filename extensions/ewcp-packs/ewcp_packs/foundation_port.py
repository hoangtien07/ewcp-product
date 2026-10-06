"""EWCP general-lane FoundationPort — fork-side adapter (M-EA1).

Wraps the embedded ``DeerFlowClient`` so the EWCP kernel drives the
deer-flow harness as the governed general agent (kernel spec
``docs/specs/005-foundation-agent-slice.md`` §2.5). One WorkRun maps to
one deer-flow thread (``thread_id="ewcp-"+workrun_id``,
``user_id="ewcp-"+tenant_id`` — both minted kernel-side and passed in
here); each attempt is one ``stream()`` turn consumed to exhaustion.

RECONCILE notes (kernel-side modules are still being merged by another
worker; everything below self-activates when they land):

- ``TurnResult`` is imported from ``ewcp.foundation.port`` when the
  kernel module exists; otherwise a spec-identical local mirror is used
  (same field names — duck-compatible either way).
- ``plugin.py`` passes ``foundation_port``/``foundation_cfg`` into
  ``create_app`` only when its signature accepts them, so the port wires
  up automatically the moment the kernel merges it.

Spec deviation, verified against ``client.py``: ``stream()`` kwargs do
NOT forward ``non_interactive``/``disable_clarification`` — only
``_EMBEDDED_AUTHORIZATION_CONTEXT_KEYS`` reach ``runtime.context``, and
``_get_runnable_config`` whitelists its own keys (client.py:85-95,
944-957). The flags are honored through two channels instead:

1. ``EwcpDeerFlowClient._get_tools`` restricts the assembled toolset to
   the file+bash groups, disables MCP/ACP tools, forces
   ``subagent_enabled=False`` and drops the ``ask_clarification`` /
   ``view_image`` builtins (spec §9 lane profile).
2. ``_EwcpRunFlagsMiddleware`` stamps ``disable_clarification`` and
   ``non_interactive`` into ``runtime.context`` at ``before_agent`` —
   the exact keys ``ClarificationMiddleware._clarification_disabled``
   and ``SandboxMiddleware._network_approval_is_non_interactive`` read.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware

logger = logging.getLogger(__name__)

EWCP_AGENT_NAME = "ewcp-general"

# M-EA3: the lane's default model profile — a `use:`-class profile in
# config.yaml pointing at ewcp_packs.governed_model.GovernedChatModel.
# An unset turn model resolves here so a governed run can never fall
# back to a direct-provider profile by default.
EWCP_GOVERNED_MODEL_NAME = "ewcp-governed"
# config.yaml tool groups the lane may assemble — the sandbox file/bash
# tool set. Web/browser/knowledge/conversation tools stay out.
EWCP_TOOL_GROUPS = ("file:read", "file:write", "bash")
# Spec §9 lane profile: no clarification tool (non-interactive), no
# vision tool, no subagent delegation, no MCP.
_DISABLED_BUILTIN_TOOLS = frozenset({"ask_clarification", "view_image"})

# ---------------------------------------------------------------------------
# Kernel wire types (RECONCILE — see module docstring)
# ---------------------------------------------------------------------------

try:  # pragma: no cover - depends on which kernel build is mounted
    from ewcp.foundation.port import TurnResult  # type: ignore[import-untyped]
except Exception:  # noqa: BLE001 — kernel pre-merge: local spec mirror

    @dataclass
    class TurnResult:  # spec 005 §2.5 — keep field names identical
        final_text: str
        usage: dict  # {"input_tokens": int, "output_tokens": int}
        stop_reason: str  # "end" | "timeout" | "error:<cls>"


def load_foundation_cfg() -> Any | None:
    """Return the kernel's ``FoundationConfig.from_env()`` when available.

    ``None`` is a valid value for a port-accepting ``create_app`` (the
    kernel applies its own default) — it only means the kernel-side
    module has not merged yet, in which case the signature gate in
    ``plugin.py`` won't have passed the port either.
    """
    try:
        from ewcp.foundation.port import FoundationConfig
    except Exception:  # noqa: BLE001
        return None
    # The kernel builds workspaces as {base}/users/ewcp-<tenant>/threads/
    # ewcp-<wid>/user-data — this MUST resolve to the harness user-data
    # root (sandbox tools map /mnt/user-data/ there) or the agent's
    # outbox/uploads/outputs land in a directory the kernel never sees.
    # Default it to the dir that owns the harness users/ tree; an
    # explicit env override still wins.
    from deerflow.config.paths import get_paths

    os.environ.setdefault("EWCP_FOUNDATION_WORKSPACE_BASE", str(get_paths().base_dir))
    try:
        return FoundationConfig.from_env()
    except AttributeError:
        return FoundationConfig()
    except Exception:
        logger.exception("FoundationConfig.from_env() failed")
        return None


# ---------------------------------------------------------------------------
# Client profile
# ---------------------------------------------------------------------------


class _EwcpRunFlagsMiddleware(AgentMiddleware):
    """Stamp EWCP non-interactive flags into ``runtime.context``.

    Both ``disable_clarification`` (ClarificationMiddleware) and
    ``non_interactive`` (SandboxMiddleware network-approval drain) are
    runtime-context keys on the embedded path — mutating the context
    dict at agent start covers every downstream reader.
    """

    def before_agent(self, state, runtime):  # noqa: ANN001, ANN201, ARG002
        context = getattr(runtime, "context", None)
        if isinstance(context, MutableMapping):
            context["disable_clarification"] = True
            context["non_interactive"] = True
        return None


from deerflow.client import DeerFlowClient  # noqa: E402


class EwcpDeerFlowClient(DeerFlowClient):
    """Embedded client pinned to the governed ewcp-general lane profile.

    Only overrides the two seams ``DeerFlowClient`` exposes for
    construction-time control — the spec's kwargs assumption was verified
    absent (see module docstring).
    """

    @staticmethod
    def _get_tools(*, model_name: str | None, subagent_enabled: bool):  # noqa: ANN202, ARG004
        # _ensure_agent calls ``self._get_tools(...)`` for every build.
        from deerflow.tools import get_available_tools

        tools = get_available_tools(
            groups=list(EWCP_TOOL_GROUPS),
            include_mcp=False,
            model_name=model_name,
            subagent_enabled=False,  # spec §9 — never widen to the caller's flag
            include_conversation_reader=False,
        )
        return [t for t in tools if t.name not in _DISABLED_BUILTIN_TOOLS]

    def _get_runnable_config(self, thread_id: str, **overrides):
        cfg = super()._get_runnable_config(thread_id, **overrides)
        cfg["configurable"]["non_interactive"] = True
        cfg["configurable"]["max_total_subagents"] = 0
        # M-EA3: the WorkRun's LLM virtual key rides the same channel as
        # model_name — GovernedChatModel resolves it via ensure_config()
        key = overrides.get("ewcp_model_key")
        if key:
            cfg["configurable"]["ewcp_model_key"] = key
        return cfg


_EWCP_AGENT_CONFIG_YAML = """\
# EWCP general lane — governed agent profile (spec 005 §10.3).
# Managed by the ewcp_packs extension: edit in the extension source, not here.
name: ewcp-general
display_name: "EWCP General Agent"
description: "Governed general-lane execution agent (acceptance-contract lane)"
# Note: on the embedded DeerFlowClient path only `memory_enabled` and the
# SOUL.md body are consumed from this profile — the tool/subagent
# restrictions below document intent and bind if the profile is ever
# loaded through the Gateway make_lead_agent path.
tool_groups:
  - file:read
  - file:write
  - bash
allowed_subagents: []
memory_enabled: false
"""

_EWCP_SOUL_MD = """\
You are the EWCP general-lane execution agent — a governed worker running
inside a per-run workspace. The kernel seeds `ewcp/` under your workspace
and sends instructions with each turn; follow them exactly.

Rules that always hold, whatever the turn says:

- The file tools only accept absolute virtual paths under
  `/mnt/user-data/`. Kernel workspace paths map like this:
    - `ewcp/<x>`  -> `/mnt/user-data/workspace/ewcp/<x>`
      (outbox envelopes, state.json, inbox, catalog, capabilities)
    - `uploads/<f>` -> `/mnt/user-data/uploads/<f>`  (read-only inputs)
    - `outputs/<f>` -> `/mnt/user-data/outputs/<f>`  (deliverables)
  Bare or relative paths (`ewcp/outbox/001-...`) are rejected — always
  write the full `/mnt/user-data/...` path.
- Talk to the kernel ONLY by writing JSON envelopes at
  `ewcp/outbox/<NNN>-<type>.json`. Every envelope MUST have exactly this
  shape — top-level `id`, `type`, `payload`, nothing else:
  `{"id": "001", "type": "propose_contract", "payload": {"criteria": [
    {"id": "rows", "kind": "row_count",
     "params": {"path": "0000-data.csv", "base": "uploads", "min": 1},
     "required": true, "label": "..."}]}}`
  Valid types: propose_contract, ask_human, invoke_capability,
  declare_done. Criterion `params.path` is a RELATIVE name under
  `params.base` — `"uploads"` for user inputs, `"outputs"` (the
  default when base is omitted) for deliverables, `"workspace"` for
  workspace files. Never an absolute host path. Never claim
  completion in prose — declare_done is the only completion signal
  the kernel accepts.
- Keep `ewcp/state.json` current (goal, plan steps, facts, open
  questions) — it is your memory across attempts.
- Put every deliverable under `outputs/` and declare each in the
  declare_done payload (path relative to the workspace, kind, label).
- Read `ewcp/inbox/` before retrying a rejected envelope — kernel error
  replies land there.
- You have no network access, no subagents, and no user to clarify with
  mid-turn — ask the human through an ask_human envelope instead.
"""


def ensure_agent_profile(base_dir: Path | None = None) -> Path:
    """Materialize the shared ``ewcp-general`` agent profile if missing.

    Writes into the legacy shared layout ``{base_dir}/agents/ewcp-general/``
    so every ``ewcp-<tenant>`` user bucket resolves the same profile
    (``resolve_agent_dir`` falls back to it). Never overwrites existing
    files — operator edits win.
    """
    if base_dir is None:
        from deerflow.config.paths import get_paths

        base_dir = Path(get_paths().base_dir)
    agent_dir = base_dir / "agents" / EWCP_AGENT_NAME
    config = agent_dir / "config.yaml"
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(_EWCP_AGENT_CONFIG_YAML, encoding="utf-8")
    soul = agent_dir / "SOUL.md"
    if not soul.exists():
        soul.write_text(_EWCP_SOUL_MD, encoding="utf-8")
    return agent_dir


# ---------------------------------------------------------------------------
# Port implementation
# ---------------------------------------------------------------------------


@dataclass
class _UserCtx:
    """Minimal ``CurrentUser`` (runtime/user_context.py wants ``.id``)."""

    id: str


def _final_text(chunks: dict[str, list[str]], last_id: str) -> str:
    """Mirror ``DeerFlowClient.chat()`` — the LAST message id's text."""
    return "".join(chunks.get(last_id, ()))


def _usage(u: Any) -> dict:
    if not isinstance(u, dict):
        u = {}
    return {
        "input_tokens": int(u.get("input_tokens") or 0),
        "output_tokens": int(u.get("output_tokens") or 0),
    }


class DeerFlowPortImpl:
    """``FoundationPort`` over the embedded ``DeerFlowClient``.

    The kernel executor consumes this synchronously inside
    ``run_in_threadpool`` — ``stream()`` is a sync generator, so
    ``turn()`` blocks the calling thread until the turn ends, times out
    (deadline checked between events; on expiry ``gen.close()``), or
    raises. ``abort()`` closes the live generator from another thread —
    best-effort: an in-flight tool call finishes or dies with the turn.
    """

    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            # the shared profile dir must exist before _ensure_agent can
            # resolve agent_name against it (per-user bucket first, shared
            # agents/ fallback — covers every ewcp-<tenant> identity)
            ensure_agent_profile()
            client = EwcpDeerFlowClient(
                agent_name=EWCP_AGENT_NAME,
                subagent_enabled=False,
                middlewares=[_EwcpRunFlagsMiddleware()],
            )
        self._client = client
        self._lock = threading.Lock()
        self._turns: dict[str, Any] = {}

    # spec §2.5 signature (+ M-EA3 virtual_key)
    def turn(
        self,
        *,
        thread_id: str,
        user_id: str,
        message: str,
        workspace: Path,
        model: str | None,
        timeout_s: float,
        virtual_key: str | None = None,
    ) -> TurnResult:
        # `workspace` is informational — the harness derives the same
        # user-data root from (thread_id, user_id) via config/paths.py.
        logger.debug("ewcp turn start thread=%s user=%s workspace=%s", thread_id, user_id, workspace)
        kwargs: dict[str, Any] = {"user_id": user_id, "is_internal": True}
        # invariant 7: unset model resolves the governed profile — a
        # turn without a virtual key still lands on GovernedChatModel,
        # which denies before any provider call (fail-closed)
        kwargs["model_name"] = model or EWCP_GOVERNED_MODEL_NAME
        if virtual_key:
            kwargs["ewcp_model_key"] = virtual_key
        gen = self._client.stream(message, thread_id=thread_id, **kwargs)

        deadline = time.monotonic() + timeout_s
        chunks: dict[str, list[str]] = {}
        last_id = ""
        usage: dict = {}
        with self._lock:
            self._turns[thread_id] = gen
        try:
            for event in gen:
                data = getattr(event, "data", None) or {}
                if event.type == "messages-tuple" and data.get("type") == "ai":
                    delta = data.get("content") or ""
                    if delta:
                        msg_id = data.get("id") or ""
                        chunks.setdefault(msg_id, []).append(str(delta))
                        last_id = msg_id
                elif event.type == "end":
                    usage = data.get("usage") or usage
                if time.monotonic() > deadline:
                    self._close_quietly(gen)
                    return TurnResult(
                        final_text=_final_text(chunks, last_id),
                        usage=_usage(usage),
                        stop_reason="timeout",
                    )
            return TurnResult(
                final_text=_final_text(chunks, last_id),
                usage=_usage(usage),
                stop_reason="end",
            )
        except Exception as exc:  # noqa: BLE001 — error is the wire value
            logger.warning("ewcp turn raised %s: %s", type(exc).__name__, exc)
            return TurnResult(
                final_text=_final_text(chunks, last_id),
                usage=_usage(usage),
                stop_reason=f"error:{type(exc).__name__}",
            )
        finally:
            with self._lock:
                self._turns.pop(thread_id, None)

    def upload_files(self, *, thread_id: str, user_id: str, files: list[Path]) -> None:
        # client.upload_files resolves the per-user uploads dir through
        # the request ContextVar — scope the ewcp user id for the call.
        from deerflow.runtime.user_context import (
            reset_current_user,
            set_current_user,
        )

        token = set_current_user(_UserCtx(user_id))
        try:
            self._client.upload_files(thread_id, [str(f) for f in files])
        finally:
            reset_current_user(token)

    def abort(self, *, thread_id: str) -> None:
        with self._lock:
            gen = self._turns.get(thread_id)
        if gen is not None:
            self._close_quietly(gen)

    @staticmethod
    def _close_quietly(gen) -> None:
        # ValueError: generator already executing (abort raced the worker
        # thread mid-next()); RuntimeError/GeneratorExit are swallowed too
        # — abort is best-effort per spec §13.
        try:
            gen.close()
        except (ValueError, RuntimeError, GeneratorExit):
            logger.debug("abort: generator already running/finished", exc_info=True)
