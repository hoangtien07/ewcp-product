"""Tests for ToolArgAliasMiddleware — measured small-model arg-name drift (C03 Ollama spike).

qwen2.5:7b-instruct deterministically emits ``contents`` for ``write_file``'s
``content`` parameter (plus a hallucinated ``mode`` extra). The middleware
normalizes a small, per-tool alias map *before* schema validation so such calls
dispatch instead of dying on "Field required". Exact-match aliases only: an
unknown arg name must still reach validation and error, so typos can never be
swallowed into the wrong field.
"""

import logging

import pytest
from langchain_core.messages import ToolMessage

from deerflow.agents.middlewares.tool_arg_alias_middleware import ToolArgAliasMiddleware
from deerflow.sandbox.tools import write_file_tool


class _FakeToolCallRequest:
    """Minimal stand-in mirroring the ToolCallRequest surface the middleware uses."""

    def __init__(self, tool_call: dict, state: dict | None = None) -> None:
        self.tool_call = tool_call
        self.state = state or {}

    def override(self, **overrides):
        return _FakeToolCallRequest(
            overrides.get("tool_call", self.tool_call),
            overrides.get("state", self.state),
        )


def _request(name: str, args) -> _FakeToolCallRequest:
    return _FakeToolCallRequest({"name": name, "args": args, "id": "call_1", "type": "tool_call"})


def _capturing_handler(seen: dict):
    def handler(req):
        seen["args"] = req.tool_call["args"]
        return ToolMessage(content="ok", tool_call_id="call_1")

    return handler


class TestWriteFileAliases:
    def test_c03_probe_args_dispatch(self):
        """The exact failing payload from the C03 spike: contents -> content, file_path -> path, hallucinated `mode` preserved as an ignored extra."""
        middleware = ToolArgAliasMiddleware()
        request = _request(
            "write_file",
            {"path": "/mnt/user-data/workspace/probe.txt", "mode": "w", "contents": "c03-probe-42"},
        )
        seen = {}

        result = middleware.wrap_tool_call(request, _capturing_handler(seen))

        assert seen["args"] == {
            "path": "/mnt/user-data/workspace/probe.txt",
            "mode": "w",
            "content": "c03-probe-42",
        }
        # The normalized args pass the model-visible schema — the tool executes.
        write_file_tool.tool_call_schema.model_validate(seen["args"])
        assert result.content == "ok"

    def test_file_path_alias(self):
        middleware = ToolArgAliasMiddleware()
        request = _request("write_file", {"file_path": "/x", "content": "y"})
        seen = {}

        middleware.wrap_tool_call(request, _capturing_handler(seen))

        assert seen["args"] == {"path": "/x", "content": "y"}

    @pytest.mark.parametrize("alias", ["contents", "text", "body", "data"])
    def test_content_aliases(self, alias: str):
        middleware = ToolArgAliasMiddleware()
        request = _request("write_file", {"path": "/x", alias: "y"})
        seen = {}

        middleware.wrap_tool_call(request, _capturing_handler(seen))

        assert seen["args"] == {"path": "/x", "content": "y"}

    def test_alias_never_overwrites_canonical(self):
        middleware = ToolArgAliasMiddleware()
        args = {"path": "/x", "content": "canonical", "contents": "drifted"}
        request = _request("write_file", args)
        seen = {}

        middleware.wrap_tool_call(request, _capturing_handler(seen))

        assert seen["args"] == args

    def test_first_alias_candidate_wins(self):
        middleware = ToolArgAliasMiddleware()
        request = _request("write_file", {"path": "/x", "text": "a", "body": "b"})
        seen = {}

        middleware.wrap_tool_call(request, _capturing_handler(seen))

        assert seen["args"]["content"] == "a"


class TestOtherFileTools:
    @pytest.mark.parametrize("tool", ["read_file", "str_replace"])
    @pytest.mark.parametrize("alias", ["file_path", "filepath", "filename", "file_name"])
    def test_path_aliases(self, tool: str, alias: str):
        middleware = ToolArgAliasMiddleware()
        args = {alias: "/x"}
        if tool == "str_replace":
            args.update({"old_str": "a", "new_str": "b"})
        request = _request(tool, args)
        seen = {}

        middleware.wrap_tool_call(request, _capturing_handler(seen))

        assert seen["args"]["path"] == "/x"
        assert alias not in seen["args"]


class TestNonAliasCallsUnchanged:
    def test_canonical_call_passes_through_unmodified(self):
        middleware = ToolArgAliasMiddleware()
        args = {"path": "/x", "content": "y", "append": True}
        request = _request("write_file", args)

        seen = {}

        def handler(req):
            seen["req"] = req
            return ToolMessage(content="ok", tool_call_id="call_1")

        middleware.wrap_tool_call(request, handler)

        assert seen["req"] is request  # no override object created
        assert request.tool_call["args"] == args

    def test_unmapped_tool_untouched(self):
        """Only the measured file r/w tools are normalized — other tools' args pass through, including plausible drift like bash's cmd."""
        middleware = ToolArgAliasMiddleware()
        request = _request("bash", {"cmd": "ls"})

        seen = {}

        def handler(req):
            seen["req"] = req
            return ToolMessage(content="ok", tool_call_id="call_1")

        middleware.wrap_tool_call(request, handler)

        assert seen["req"] is request

    def test_unknown_tool_call_args_non_dict(self):
        middleware = ToolArgAliasMiddleware()
        request = _request("write_file", "not-a-dict")

        seen = {}

        def handler(req):
            seen["req"] = req
            return ToolMessage(content="ok", tool_call_id="call_1")

        middleware.wrap_tool_call(request, handler)

        assert seen["req"] is request


class TestUnknownArgsStillError:
    def test_typo_not_in_alias_map_is_preserved(self):
        """`contnet` is a typo, not a registered alias — it must stay in args so validation still reports the missing required field."""
        middleware = ToolArgAliasMiddleware()
        request = _request("write_file", {"path": "/x", "contnet": "y"})
        seen = {}

        middleware.wrap_tool_call(request, _capturing_handler(seen))

        assert seen["args"] == {"path": "/x", "contnet": "y"}
        with pytest.raises(Exception):
            write_file_tool.tool_call_schema.model_validate(seen["args"])


class TestLogging:
    def test_alias_rewrite_logged(self, caplog):
        middleware = ToolArgAliasMiddleware()
        request = _request("write_file", {"path": "/x", "contents": "y"})

        with caplog.at_level(logging.INFO, logger="deerflow.agents.middlewares.tool_arg_alias_middleware"):
            middleware.wrap_tool_call(request, _capturing_handler({}))

        records = [r for r in caplog.records if getattr(r, "arg_alias_applied", None)]
        assert len(records) == 1
        assert records[0].arg_alias_applied == {"contents": "content"}

    def test_no_rewrite_no_log(self, caplog):
        middleware = ToolArgAliasMiddleware()
        request = _request("write_file", {"path": "/x", "content": "y"})

        with caplog.at_level(logging.INFO, logger="deerflow.agents.middlewares.tool_arg_alias_middleware"):
            middleware.wrap_tool_call(request, _capturing_handler({}))

        assert not [r for r in caplog.records if getattr(r, "arg_alias_applied", None)]


class TestAsyncPath:
    @pytest.mark.asyncio
    async def test_awrap_tool_call_normalizes(self):
        middleware = ToolArgAliasMiddleware()
        request = _request("write_file", {"file_path": "/x", "contents": "y"})
        seen = {}

        async def handler(req):
            seen["args"] = req.tool_call["args"]
            return ToolMessage(content="ok", tool_call_id="call_1")

        await middleware.awrap_tool_call(request, handler)

        assert seen["args"] == {"path": "/x", "content": "y"}


class TestRegistration:
    def test_registered_before_argument_policies(self):
        """The alias layer must run before artifact resolution, guardrails, audit and the read-before-write gate so they inspect canonical arg names."""
        from deerflow.agents.middlewares.tool_error_handling_middleware import build_lead_runtime_middlewares
        from deerflow.config.app_config import AppConfig
        from deerflow.config.sandbox_config import SandboxConfig

        middlewares = build_lead_runtime_middlewares(app_config=AppConfig(sandbox=SandboxConfig(use="test")))
        types = [type(m).__name__ for m in middlewares]

        assert "ToolArgAliasMiddleware" in types
        alias_index = types.index("ToolArgAliasMiddleware")
        for inner in ("ArtifactResolutionMiddleware", "ReadBeforeWriteMiddleware", "SandboxAuditMiddleware", "ToolErrorHandlingMiddleware"):
            if inner in types:
                assert alias_index < types.index(inner), f"ToolArgAliasMiddleware must be outer of {inner}"
