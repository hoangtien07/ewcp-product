"""Tests for resolving semantic placements into real stack positions."""

from __future__ import annotations

from deerflow_extension_api import (
    AgentBuildContext,
    AgentScope,
    MiddlewarePlacement,
    Placement,
)
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from deerflow.extensions.anchors import PlacementAnchor, inner_of, innermost, outer_of, outermost
from deerflow.extensions.injection import inject_middlewares
from deerflow.extensions.registry import ExtensionRegistry


class _Core:
    """Stand-in for a core middleware; only its type matters for anchoring."""


class _Retry(_Core):
    pass


class _Transform(_Core):
    pass


class _ToolError(_Core):
    pass


class _Last(_Core):
    pass


class _Probe(AgentMiddleware):
    def __init__(self, tag: str) -> None:
        super().__init__()
        self.tag = tag


class _NamedCoreMiddleware(AgentMiddleware):
    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    @property
    def name(self) -> str:
        return self._name


class _FakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):  # type: ignore[override]
        return self


_ANCHORS = {
    Placement.MODEL_LOGICAL: outer_of(_Retry),
    Placement.MODEL_PHYSICAL: inner_of(_Transform),
    Placement.TOOL_VISIBLE: outermost(),
    Placement.TOOL_RAW: inner_of(_ToolError),
    Placement.STANDARD: outer_of(_Last),
}


def _stack() -> list[object]:
    return [_Retry(), _Transform(), _ToolError(), _Last()]


def _contributor(*placements: MiddlewarePlacement):
    class _C:
        def contribute_middlewares(self, app_store, ctx):
            return placements

    return _C()


def _extensions(*placements: MiddlewarePlacement):
    registry = ExtensionRegistry()
    with registry.attributed_to("demo:install"):
        registry.middlewares(_contributor(*placements))
    return registry.build()


def _ctx() -> AgentBuildContext:
    return AgentBuildContext(scope=AgentScope.LEAD)


def _tags(stack: list[object]) -> list[str]:
    from deerflow.extensions.isolation import IsolatedMiddleware

    out = []
    for m in stack:
        target = m.inner if isinstance(m, IsolatedMiddleware) else m
        out.append(target.tag if isinstance(target, _Probe) else type(target).__name__)
    return out


def test_outermost_lands_at_index_zero():
    probe = _Probe("visible")
    result, _, _ = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(MiddlewarePlacement(probe, Placement.TOOL_VISIBLE)),
    )
    assert _tags(result)[0] == "visible"


def test_outer_of_lands_immediately_before_the_anchor():
    probe = _Probe("decision")
    result, _, _ = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(MiddlewarePlacement(probe, Placement.MODEL_LOGICAL)),
    )
    assert _tags(result) == ["decision", "_Retry", "_Transform", "_ToolError", "_Last"]


def test_inner_of_lands_immediately_after_the_anchor():
    probe = _Probe("attempt")
    result, _, _ = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(MiddlewarePlacement(probe, Placement.MODEL_PHYSICAL)),
    )
    assert _tags(result) == ["_Retry", "_Transform", "attempt", "_ToolError", "_Last"]


def test_all_four_placements_nest_correctly():
    result, _, _ = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(
            MiddlewarePlacement(_Probe("visible"), Placement.TOOL_VISIBLE),
            MiddlewarePlacement(_Probe("decision"), Placement.MODEL_LOGICAL),
            MiddlewarePlacement(_Probe("attempt"), Placement.MODEL_PHYSICAL),
            MiddlewarePlacement(_Probe("raw"), Placement.TOOL_RAW),
        ),
    )
    assert _tags(result) == [
        "visible",
        "decision",
        "_Retry",
        "_Transform",
        "attempt",
        "_ToolError",
        "raw",
        "_Last",
    ]


def test_multiple_extension_middlewares_can_compile_into_one_agent():
    result, _, _ = inject_middlewares(
        [],
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(
            MiddlewarePlacement(_Probe("first"), Placement.TOOL_VISIBLE),
            MiddlewarePlacement(_Probe("second"), Placement.TOOL_VISIBLE),
        ),
    )

    agent = create_agent(
        _FakeModel(responses=[AIMessage(content="ok")]),
        middleware=result,
    )

    assert agent is not None


def test_extension_middleware_names_avoid_langgraph_reserved_characters():
    result, _, _ = inject_middlewares(
        [],
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(MiddlewarePlacement(_Probe("probe"), Placement.TOOL_VISIBLE)),
    )

    names = [middleware.name for middleware in result]
    assert all(":" not in name and "|" not in name for name in names)


def test_extension_middleware_names_are_stable_across_equivalent_builds():
    def build_names() -> list[str]:
        result, _, _ = inject_middlewares(
            [],
            _ANCHORS,
            AgentScope.LEAD,
            _ctx(),
            _extensions(
                MiddlewarePlacement(_Probe("first"), Placement.TOOL_VISIBLE),
                MiddlewarePlacement(_Probe("second"), Placement.TOOL_VISIBLE),
            ),
        )
        return [middleware.name for middleware in result]

    assert build_names() == build_names()


def test_extension_middleware_name_does_not_collide_with_the_core_stack():
    placement = MiddlewarePlacement(_Probe("probe"), Placement.TOOL_VISIBLE)
    first_result, _, _ = inject_middlewares(
        [],
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(placement),
    )
    core = _NamedCoreMiddleware(first_result[0].name)

    result, _, _ = inject_middlewares(
        [core],
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(placement),
    )

    agent = create_agent(
        _FakeModel(responses=[AIMessage(content="ok")]),
        middleware=result,
    )
    assert agent is not None


def test_scope_filters_out_non_matching_contributions():
    result, _, _ = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.SUBAGENT,
        _ctx(),
        _extensions(MiddlewarePlacement(_Probe("lead-only"), Placement.STANDARD, scope=AgentScope.LEAD)),
    )
    assert "lead-only" not in _tags(result)


def test_scope_both_applies_everywhere():
    for scope in (AgentScope.LEAD, AgentScope.SUBAGENT):
        result, _, _ = inject_middlewares(
            _stack(),
            _ANCHORS,
            scope,
            _ctx(),
            _extensions(MiddlewarePlacement(_Probe("everywhere"), Placement.STANDARD)),
        )
        assert "everywhere" in _tags(result)


def test_order_field_breaks_ties_within_a_placement():
    result, _, _ = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(
            MiddlewarePlacement(_Probe("second"), Placement.TOOL_VISIBLE, order=10),
            MiddlewarePlacement(_Probe("first"), Placement.TOOL_VISIBLE, order=1),
        ),
    )
    assert _tags(result)[:2] == ["first", "second"]


def test_provenance_maps_index_to_source():
    probe = _Probe("visible")
    result, provenance, _ = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(MiddlewarePlacement(probe, Placement.TOOL_VISIBLE)),
    )
    assert provenance[0] == "demo:install"
    assert 1 not in provenance, "core middlewares carry no extension provenance"


def test_missing_anchor_falls_back_and_warns():
    """A conditionally-built stack may lack the anchor. Degrading silently
    would change what the extension observes with no signal."""
    anchors = {Placement.MODEL_PHYSICAL: PlacementAnchor.of(inner_of(_Transform), innermost())}
    stack = [_Retry(), _ToolError()]  # no _Transform
    result, _, diagnostics = inject_middlewares(
        stack,
        anchors,
        AgentScope.LEAD,
        _ctx(),
        _extensions(MiddlewarePlacement(_Probe("attempt"), Placement.MODEL_PHYSICAL)),
    )
    assert _tags(result)[-1] == "attempt"
    assert [d.level for d in diagnostics] == ["warning"]
    assert "MODEL_PHYSICAL" in diagnostics[0].message


def test_no_contributors_returns_the_stack_untouched():
    stack = _stack()
    result, provenance, diagnostics = inject_middlewares(stack, _ANCHORS, AgentScope.LEAD, _ctx(), ExtensionRegistry().build())
    assert result == stack
    assert provenance == {}
    assert diagnostics == []


def test_contributor_failure_is_isolated_to_that_extension():
    class _Boom:
        def contribute_middlewares(self, app_store, ctx):
            raise ValueError("contributor exploded")

    registry = ExtensionRegistry()
    with registry.attributed_to("bad:install"):
        registry.middlewares(_Boom())
    with registry.attributed_to("good:install"):
        registry.middlewares(_contributor(MiddlewarePlacement(_Probe("ok"), Placement.TOOL_VISIBLE)))

    result, _, diagnostics = inject_middlewares(_stack(), _ANCHORS, AgentScope.LEAD, _ctx(), registry.build())
    assert "ok" in _tags(result)
    assert any(d.level == "error" and d.source == "bad:install" for d in diagnostics)


def test_contributor_iterable_failure_is_isolated_to_that_extension():
    class _ExplodingIterable:
        def __iter__(self):
            raise ValueError("iteration exploded")

    class _Boom:
        def contribute_middlewares(self, app_store, ctx):
            return _ExplodingIterable()

    registry = ExtensionRegistry()
    with registry.attributed_to("bad:install"):
        registry.middlewares(_Boom())
    with registry.attributed_to("good:install"):
        registry.middlewares(_contributor(MiddlewarePlacement(_Probe("ok"), Placement.TOOL_VISIBLE)))

    result, _, diagnostics = inject_middlewares(_stack(), _ANCHORS, AgentScope.LEAD, _ctx(), registry.build())

    assert "ok" in _tags(result)
    assert any(d.source == "bad:install" and "iteration exploded" in d.message for d in diagnostics)


def test_malformed_placement_is_skipped_without_losing_other_extensions():
    class _Malformed:
        def contribute_middlewares(self, app_store, ctx):
            return (object(),)

    registry = ExtensionRegistry()
    with registry.attributed_to("bad:install"):
        registry.middlewares(_Malformed())
    with registry.attributed_to("good:install"):
        registry.middlewares(_contributor(MiddlewarePlacement(_Probe("ok"), Placement.TOOL_VISIBLE)))

    result, _, diagnostics = inject_middlewares(_stack(), _ANCHORS, AgentScope.LEAD, _ctx(), registry.build())

    assert "ok" in _tags(result)
    assert any(d.source == "bad:install" and "MiddlewarePlacement" in d.message for d in diagnostics)


def test_non_middleware_contribution_is_skipped():
    result, _, diagnostics = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(MiddlewarePlacement(object(), Placement.TOOL_VISIBLE)),
    )

    assert len(result) == len(_stack())
    assert any("AgentMiddleware" in diagnostic.message for diagnostic in diagnostics)


def test_wrapper_construction_failure_is_isolated_to_one_contribution():
    class _BadName(AgentMiddleware):
        @property
        def name(self):
            raise ValueError("name exploded")

    result, _, diagnostics = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(
            MiddlewarePlacement(_BadName(), Placement.TOOL_VISIBLE),
            MiddlewarePlacement(_Probe("ok"), Placement.TOOL_VISIBLE),
        ),
    )

    assert "ok" in _tags(result)
    assert any("name exploded" in diagnostic.message for diagnostic in diagnostics)


# -- intercepting contributions (deny decisions opt out of isolation) ----------


class _DenyingMiddleware(AgentMiddleware):
    """A decision middleware: raises instead of calling the handler."""

    def __init__(self) -> None:
        super().__init__()
        self.admit_calls = 0

    async def awrap_model_call(self, request, handler):
        raise PermissionError("budget admission denied")


def test_intercepting_contribution_is_not_isolated():
    """`intercepting=True` opts out of IsolatedMiddleware: the raw
    middleware lands in the stack so its exceptions actually propagate —
    a deny that the isolation wrapper would have swallowed."""
    from deerflow.extensions.isolation import IsolatedMiddleware

    result, _, _ = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(MiddlewarePlacement(_DenyingMiddleware(), Placement.MODEL_PHYSICAL, intercepting=True)),
    )

    intercepting = [m for m in result if isinstance(m, _DenyingMiddleware)]
    assert len(intercepting) == 1
    assert all(not isinstance(m, IsolatedMiddleware) for m in result if isinstance(m, _DenyingMiddleware) or (isinstance(m, IsolatedMiddleware) and isinstance(m.inner, _DenyingMiddleware)))


def test_intercepting_middleware_exception_propagates_through_stack():
    """Fail-closed means the deny reaches the graph's error policy — under
    the isolation wrapper it would have been swallowed into a pass-through."""
    import asyncio

    from deerflow.extensions.isolation import IsolatedMiddleware

    result, _, _ = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(MiddlewarePlacement(_DenyingMiddleware(), Placement.MODEL_PHYSICAL, intercepting=True)),
    )

    entry = next(m for m in result if isinstance(m, _DenyingMiddleware))
    assert not isinstance(entry, IsolatedMiddleware)

    handler_called = False

    async def handler(request):
        nonlocal handler_called
        handler_called = True

    import pytest

    with pytest.raises(PermissionError, match="budget admission denied"):
        asyncio.run(entry.awrap_model_call(object(), handler))
    assert handler_called is False


def test_non_intercepting_contribution_stays_isolated():
    """Regression: the default (observational) path still gets the
    fail-open wrapper — intercepting is opt-in per placement."""
    from deerflow.extensions.isolation import IsolatedMiddleware

    result, _, _ = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(MiddlewarePlacement(_DenyingMiddleware(), Placement.MODEL_PHYSICAL)),
    )

    assert any(isinstance(m, IsolatedMiddleware) and isinstance(m.inner, _DenyingMiddleware) for m in result)


def test_intercepting_name_collision_is_skipped_with_diagnostic():
    """A raw middleware cannot be renamed to fit the unique-name rule —
    colliding names reject the contribution instead of silently inserting
    a second 'same-name' middleware LangGraph would reject later."""
    result, _, diagnostics = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(
            MiddlewarePlacement(_DenyingMiddleware(), Placement.TOOL_VISIBLE, intercepting=True),
            MiddlewarePlacement(_DenyingMiddleware(), Placement.TOOL_VISIBLE, intercepting=True),
        ),
    )

    assert sum(isinstance(m, _DenyingMiddleware) for m in result) == 1
    assert any("name" in d.message.lower() and "collide" in d.message.lower() or "unique" in d.message.lower() for d in diagnostics)


def test_intercepting_provenance_records_source():
    """Ordering violations can still blame the extension — provenance must
    cover raw (unwrapped) insertions, not only IsolatedMiddleware ones."""
    result, provenance, _ = inject_middlewares(
        _stack(),
        _ANCHORS,
        AgentScope.LEAD,
        _ctx(),
        _extensions(MiddlewarePlacement(_DenyingMiddleware(), Placement.MODEL_PHYSICAL, intercepting=True)),
    )

    index = next(i for i, m in enumerate(result) if isinstance(m, _DenyingMiddleware))
    assert provenance.get(index) == "demo:install"
