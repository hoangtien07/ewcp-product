"""GovernedChatModel — M-EA3 invariant-7: every paid call must route
through the kernel ModelBroker under the run's virtual key. These tests
run against a fake broker — the provider client never exists in this
process, which is itself the property under test (the class has no
provider path to leak to)."""

from __future__ import annotations

import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import set_config_context
from langchain_core.tools import tool

from ewcp_packs import governed_model as gm
from ewcp_packs.foundation_port import EWCP_GOVERNED_MODEL_NAME

_CFG = RunnableConfig(configurable={"thread_id": "ewcp-w1", "ewcp_model_key": "vk-w1-aa"})


class _FakeBroker:
    """Spy standing in for ewcp.runtime.model_broker.ModelBroker."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, *, virtual_key, workrun_id, model, messages, **kw):
        self.calls.append(
            {
                "virtual_key": virtual_key,
                "workrun_id": workrun_id,
                "model": model,
                "messages": messages,
                "kwargs": kw,
            }
        )
        return self._responses.pop(0)


def _text_resp(text: str = "xong", **extra) -> dict:
    return {
        "text": text,
        "blocks": [{"type": "text", "text": text}],
        "tool_calls": [],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        "finish_reason": "STOP",
        **extra,
    }


def _model(monkeypatch: pytest.MonkeyPatch, broker: _FakeBroker):
    monkeypatch.setattr(gm, "_broker", lambda: broker)
    return gm.GovernedChatModel(model="gemini-test")


def _invoke(model, messages, config=_CFG):
    # mirrors the real path: langgraph runs the model node via
    # ctx.run() inside set_config_context — the contextvar is only
    # visible to callables invoked on that context
    with set_config_context(config) as ctx:
        return ctx.run(lambda: model.invoke(messages))


class TestFailClosed:
    def test_no_key_denies_before_broker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        broker = _FakeBroker([_text_resp()])
        model = _model(monkeypatch, broker)
        with pytest.raises(gm.ModelGovernanceError, match="ewcp_model_key"):
            _invoke(
                model,
                [HumanMessage(content="hi")],
                config={"configurable": {"thread_id": "ewcp-w1"}},
            )
        assert broker.calls == []  # denied before any provider wire call

    def test_no_broker_denies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise():
            raise gm.ModelGovernanceError("no broker")

        monkeypatch.setattr(gm, "_broker", _raise)
        with pytest.raises(gm.ModelGovernanceError):
            _invoke(
                gm.GovernedChatModel(model="m"),
                [HumanMessage(content="hi")],
            )


class TestCallShape:
    def test_invoke_routes_key_model_messages(self, monkeypatch: pytest.MonkeyPatch) -> None:
        broker = _FakeBroker([_text_resp("trả lời")])
        model = _model(monkeypatch, broker)
        out = _invoke(
            model,
            [
                SystemMessage(content="sys"),
                HumanMessage(content="làm việc đi"),
                ToolMessage(content="tool out", tool_call_id="call_1", name="bash"),
            ],
        )
        call = broker.calls[0]
        assert call["virtual_key"] == "vk-w1-aa"
        assert call["workrun_id"] == "w1"
        assert call["model"] == "gemini-test"
        assert call["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "làm việc đi"},
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "bash",
                "content": "tool out",
            },
        ]
        assert out.content == [{"type": "text", "text": "trả lời"}]
        assert out.usage_metadata == {
            "input_tokens": 5,
            "output_tokens": 3,
            "total_tokens": 8,
        }

    def test_stop_sequences_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        broker = _FakeBroker([_text_resp()])
        model = _model(monkeypatch, broker)
        with set_config_context(_CFG) as ctx:
            ctx.run(lambda: model.invoke([HumanMessage(content="x")], stop=["END"]))
        assert broker.calls[0]["kwargs"]["stop"] == ["END"]

    def test_workrun_id_strips_ewcp_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        broker = _FakeBroker([_text_resp()])
        model = _model(monkeypatch, broker)
        _invoke(model, [HumanMessage(content="x")])
        assert broker.calls[0]["workrun_id"] == "w1"


class TestToolCalls:
    def test_response_tool_calls_become_lc_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _text_resp(
            "",
            tool_calls=[
                {
                    "id": "call_9",
                    "name": "bash",
                    "args": {"command": "ls"},
                    "thought_signature": "c2ln",
                }
            ],
        )
        broker = _FakeBroker([resp])
        model = _model(monkeypatch, broker)
        out = _invoke(model, [HumanMessage(content="run ls")])
        assert out.tool_calls == [
            {
                "id": "call_9",
                "name": "bash",
                "args": {"command": "ls"},
                "type": "tool_call",
            }
        ]
        # verbatim payload stored for faithful replay
        stored = out.additional_kwargs[gm._EWCP_RESPONSE_KEY]
        assert stored["tool_calls"][0]["thought_signature"] == "c2ln"

    def test_assistant_replay_emits_verbatim_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = _text_resp(
            "",
            tool_calls=[
                {
                    "id": "call_9",
                    "name": "bash",
                    "args": {"command": "ls"},
                    "thought_signature": "c2ln",
                }
            ],
        )
        broker = _FakeBroker([resp, _text_resp("done")])
        model = _model(monkeypatch, broker)
        ai = _invoke(model, [HumanMessage(content="run")])
        _invoke(
            model,
            [
                HumanMessage(content="run"),
                ai,
                ToolMessage(content="ok", tool_call_id="call_9", name="bash"),
            ],
        )
        replayed = broker.calls[1]["messages"][1]
        assert replayed["tool_calls"][0]["thought_signature"] == "c2ln"
        assert replayed["tool_calls"][0]["args"] == {"command": "ls"}

    def test_gemini_sig_map_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Messages produced by ChatGoogleGenerativeAI (sig map in
        additional_kwargs) still replay their signatures."""
        ai = AIMessage(
            content="",
            tool_calls=[{"id": "call_7", "name": "bash", "args": {}}],
            additional_kwargs={gm._GEMINI_SIG_MAP_KEY: {"call_7": "ZmFrZXNpZw=="}},
        )
        ser = gm._to_kernel_message(ai)
        assert ser["tool_calls"][0]["thought_signature"] == "ZmFrZXNpZw=="

    def test_bind_tools_normalizes_schema(self, monkeypatch: pytest.MonkeyPatch) -> None:
        @tool
        def read_file(path: str) -> str:
            """Read a file."""
            return path

        broker = _FakeBroker([_text_resp()])
        model = _model(monkeypatch, broker)
        bound = model.bind_tools([read_file], tool_choice="any")
        with set_config_context(_CFG) as ctx:
            ctx.run(lambda: bound.invoke([HumanMessage(content="x")]))
        kw = broker.calls[0]["kwargs"]
        assert kw["tool_choice"] == "any"
        tools = kw["tools"]
        assert tools[0]["name"] == "read_file"
        assert tools[0]["parameters"]["properties"]["path"]["type"] == "string"


class TestStreaming:
    def test_stream_emits_text_then_tool_calls_then_usage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resp = {
            "text": "phần trả lời",
            "blocks": [
                {"type": "thinking", "thinking": "suy nghĩ", "signature": "s"},
                {"type": "text", "text": "phần trả lời"},
            ],
            "tool_calls": [{"id": "call_1", "name": "bash", "args": {"command": "x"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            "finish_reason": "STOP",
        }
        broker = _FakeBroker([resp])
        model = _model(monkeypatch, broker)
        with set_config_context(_CFG) as ctx:
            chunks = ctx.run(lambda: list(model.stream([HumanMessage(content="x")])))
        flat = [c.content for c in chunks]
        first_block = flat[0][0]
        assert first_block["type"] == "thinking"
        assert "phần trả lời" in "".join(x for x in flat if isinstance(x, str))
        # langchain may coalesce trailing chunks — find the usage carrier
        usage_chunk = next(c for c in chunks if c.usage_metadata)
        assert usage_chunk.usage_metadata["total_tokens"] == 3
        assert gm._EWCP_RESPONSE_KEY in usage_chunk.additional_kwargs


class TestAgentLoopSpy:
    """DoD spy: drive a real langchain create_agent loop — every model
    call in the loop must land on the broker; there is no other path."""

    def test_agent_loop_all_calls_via_broker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from langchain.agents import create_agent

        @tool
        def list_files(path: str) -> str:
            """List files."""
            return f"files in {path}"

        broker = _FakeBroker(
            [
                {
                    "text": "",
                    "blocks": [],
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "name": "list_files",
                            "args": {"path": "/tmp"},
                            "thought_signature": "dGhtZw==",
                        }
                    ],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
                    "finish_reason": "STOP",
                },
                _text_resp("đã liệt kê"),
            ]
        )
        monkeypatch.setattr(gm, "_broker", lambda: broker)
        model = gm.GovernedChatModel(model="gemini-test")
        agent = create_agent(model, tools=[list_files])
        out = agent.invoke(
            {"messages": [HumanMessage(content="list /tmp")]},
            config=dict(_CFG),
        )
        # both model calls (initial + post-tool) went through the broker
        assert len(broker.calls) == 2
        assert all(c["virtual_key"] == "vk-w1-aa" for c in broker.calls)
        # the replayed assistant turn carried the provider's own parts
        second = broker.calls[1]["messages"]
        assert second[1]["role"] == "assistant"
        assert second[1]["tool_calls"][0]["thought_signature"] == "dGhtZw=="
        assert second[2]["role"] == "tool"
        assert second[2]["content"] == "files in /tmp"
        assert out["messages"][-1].content == [{"type": "text", "text": "đã liệt kê"}]

    def test_agent_loop_no_key_denies_without_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from langchain.agents import create_agent

        broker = _FakeBroker([_text_resp()])
        monkeypatch.setattr(gm, "_broker", lambda: broker)
        agent = create_agent(gm.GovernedChatModel(model="m"), tools=[])
        with pytest.raises(Exception):
            agent.invoke(
                {"messages": [HumanMessage(content="hi")]},
                config={"configurable": {"thread_id": "ewcp-w9"}},
            )
        assert broker.calls == []


class TestProfileResolution:
    def test_factory_resolves_governed_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`use:` class path in config.yaml instantiates the adapter —
        the lane can never reach a provider class by name."""
        from deerflow.config.model_config import ModelConfig
        from deerflow.models.factory import create_chat_model

        cfg = ModelConfig(
            name=EWCP_GOVERNED_MODEL_NAME,
            use="ewcp_packs.governed_model:GovernedChatModel",
            model="gemini-3.1-flash-lite",
        )
        app_cfg = type("AC", (), {"get_model_config": lambda s, n: cfg})()
        model = create_chat_model(EWCP_GOVERNED_MODEL_NAME, app_config=app_cfg, attach_tracing=False)
        assert isinstance(model, gm.GovernedChatModel)
        assert model.model == "gemini-3.1-flash-lite"

    def test_factory_missing_profile_denies(self) -> None:
        from deerflow.models.factory import create_chat_model

        app_cfg = type("AC", (), {"get_model_config": lambda s, n: None})()
        with pytest.raises(Exception):
            create_chat_model(
                EWCP_GOVERNED_MODEL_NAME,
                app_config=app_cfg,
                attach_tracing=False,
            )
