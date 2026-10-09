"""Normalize small-model tool-call argument *names* before schema validation.

Measured motivation (``docs/vnext/C03_OLLAMA_SPIKE.md``): qwen2.5:7b-instruct
via Ollama deterministically emits ``contents`` where ``write_file``'s schema
requires ``content``, so every file write died on a pydantic
"Field required" error — the model then dodged into ``ask_clarification``
instead of recovering. Small local models systematically drift on argument
names while still producing well-formed, dispatchable JSON.

This middleware is a *thin* fix for that measured drift only: an exact-match,
per-tool alias map covering the file read/write tools the spike exercised.
Each alias fills its canonical field **only** when that field is absent — a
canonical name the model already emitted always wins — and only for tools in
the map. Unknown argument names are never rewritten: a typo like ``contnet``
reaches validation untouched and still errors, so the map can never swallow a
bad name into the wrong field. Renames are dropped from the args so downstream
argument-inspecting layers (artifact-handle resolution, guardrails, sandbox
audit, the read-before-write gate) all see canonical names; every applied
rename is logged under the structured field ``arg_alias_applied``.

Scope deliberately excludes every other tool (bash, task, MCP tools, ...):
remaining observed drift is recorded in the C03 doc for a later pass.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

#: canonical arg name -> accepted model-emitted aliases, per tool name.
#: Only the file r/w tools the C03 spike measured. Alias order is the
#: resolution order: the first source present wins.
_TOOL_ARG_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "write_file": {
        "content": ("contents", "text", "body", "data"),
        "path": ("file_path", "filepath", "file_name", "filename"),
    },
    "read_file": {
        "path": ("file_path", "filepath", "file_name", "filename"),
    },
    "str_replace": {
        "path": ("file_path", "filepath", "file_name", "filename"),
    },
}


def normalize_tool_call_args(tool_name: Any, args: Any) -> tuple[Any, dict[str, str] | None]:
    """Return ``(normalized_args, applied)``: renamed copy of ``args`` plus the alias->canonical map applied.

    Returns the input unchanged (``(args, None)``) when the tool has no alias
    map, args is not a dict, or nothing needed renaming. A canonical name
    already present is never overwritten; a second alias for the same field is
    left behind as an (ignored) extra.
    """
    aliases = _TOOL_ARG_ALIASES.get(tool_name) if isinstance(tool_name, str) else None
    if not aliases or not isinstance(args, dict):
        return args, None
    normalized = dict(args)
    applied: dict[str, str] = {}
    for canonical, sources in aliases.items():
        if canonical in normalized:
            continue
        for source in sources:
            if source in normalized:
                normalized[canonical] = normalized.pop(source)
                applied[source] = canonical
                break
    if not applied:
        return args, None
    return normalized, applied


class ToolArgAliasMiddleware(AgentMiddleware[AgentState]):
    """Map model-drifted arg names onto canonical schema names before validation."""

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        return handler(self._normalize(request))

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        return await handler(self._normalize(request))

    def _normalize(self, request: ToolCallRequest) -> ToolCallRequest:
        tool_call = request.tool_call
        normalized_args, applied = normalize_tool_call_args(tool_call.get("name"), tool_call.get("args"))
        if applied is None:
            return request
        logger.info(
            "tool-call arg alias rewrite for %s: %s",
            tool_call.get("name"),
            applied,
            extra={"arg_alias_applied": applied},
        )
        return request.override(tool_call={**tool_call, "args": normalized_args})
