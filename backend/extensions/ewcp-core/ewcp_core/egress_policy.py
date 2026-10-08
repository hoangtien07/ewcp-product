"""EWCP egress policy — per-tenant/run data-egress enforcement (A3 Task 4).

Channels and their real hooks (inventory on product/vnext):

- **Model** — ``awrap_model_call`` contributed at ``Placement.MODEL_PHYSICAL``;
  fires once per physical provider call, retries re-enter. Denial returns an
  ``AIMessage`` refusal instead of calling ``handler`` so no request body
  leaves the boundary.
- **Tool** — ``awrap_tool_call`` contributed at ``Placement.TOOL_RAW``
  (adjacent to the callable); denial returns an error ``ToolMessage`` without
  invoking the tool.
- **Sandbox network** — NOT a middleware hook. Enforced by capability
  evaluation of the configured sandbox provider
  (``runtime.context["app_config"].sandbox``): only ``AioSandboxProvider``'s
  local Docker backend implements ``isolated``/``allowlist`` network modes;
  ``LocalSandboxProvider`` with ``allow_host_bash: false`` has no egress path.
  Middleware policy is not network isolation — this seam is evaluated
  per-mode and enforced fail-closed at ``abefore_agent``.

Fail-closed contract: for ``local_only``/``restricted`` modes, a mandatory
data path that cannot be proven enforceable denies the call; a sandbox-net
path that cannot be enforced fails the whole run before it starts
(``EgressDeniedError`` → ``GraphBubbleUp`` → propagates through the error
middleware). Unknown tenants classify ``sensitive``; ``non_sensitive``
tenants run unmodified (kernel ``DataEgressPolicy`` parity, including the
``demo``/``default`` built-in allowance).
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from deerflow_extension_api.placement import AgentScope, MiddlewarePlacement, Placement
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.errors import GraphBubbleUp

# --- channels / modes --------------------------------------------------------

CHANNEL_MODEL = "model"
CHANNEL_TOOL = "tool"
CHANNEL_SANDBOX_NET = "sandbox_net"

LOCAL_ONLY = "local_only"
RESTRICTED = "restricted"
APPROVED_CLOUD = "approved_cloud"

_MODES = frozenset({LOCAL_ONLY, RESTRICTED, APPROVED_CLOUD})

_CLASS_SENSITIVE = "sensitive"
_CLASS_NON_SENSITIVE = "non_sensitive"
_CLASSES = frozenset({_CLASS_SENSITIVE, _CLASS_NON_SENSITIVE})

# Kernel parity: src/ewcp/policy/egress.py BUILTIN_ALLOW.
_BUILTIN_NON_SENSITIVE_TENANTS = frozenset({"demo", "default"})

_CTX_TENANT_ID = "ewcp_tenant_id"
_CTX_EGRESS_MODE = "ewcp_egress_mode"

_DENY_KEY = "ewcp_egress_deny"

# Env overrides (env-first, matching KernelClientConfig resolution).
_ENV_DEFAULT_MODE = "EWCP_EGRESS_DEFAULT_MODE"
_ENV_TENANT_MODES = "EWCP_EGRESS_TENANT_MODES"
_ENV_TENANT_CLASSES = "EWCP_TENANT_DATA_CLASS"  # kernel-compatible format
_ENV_LOCAL_ENDPOINTS = "EWCP_EGRESS_LOCAL_MODEL_ENDPOINTS"
_ENV_ALLOWED_MODEL_ENDPOINTS = "EWCP_EGRESS_ALLOWED_MODEL_ENDPOINTS"
_ENV_APPROVED_MODEL_ENDPOINTS = "EWCP_EGRESS_APPROVED_MODEL_ENDPOINTS"
_ENV_ALLOWED_DOMAINS = "EWCP_EGRESS_ALLOWED_DOMAINS"
_ENV_ALLOWED_TOOLS = "EWCP_EGRESS_ALLOWED_TOOLS"

# Builtin tools whose execution cannot reach the network directly.
_FILESYSTEM_TOOLS = frozenset({"bash", "ls", "read_file", "write_file", "str_replace", "glob", "grep"})
_SAFE_BUILTIN_TOOLS = frozenset(
    {
        "present_files",
        "ask_clarification",
        "view_image",
        "review_skill_package",
        "task",
        "batch_task",
        "batch_status",
        "cancel_batch",
    }
)
# Builtin tools with a network egress path. ``bash`` is handled separately —
# its egress rides the sandbox-net verdict.
_NETWORK_TOOLS = frozenset({"web_search", "web_fetch", "image_search", "web_capture"})
_NETWORK_TOOL_PREFIXES = ("browser_",)
_URL_ARG_KEYS = ("url", "uri", "endpoint")

# Model attribute names providers use to carry the endpoint URL.
_MODEL_ENDPOINT_ATTRS = (
    "base_url",
    "openai_api_base",
    "api_base",
    "azure_endpoint",
    "anthropic_api_url",
    "api_endpoint",
    "endpoint",
    "server_url",
)


class EgressDeniedError(GraphBubbleUp):
    """Hard-fail a run when a mandatory egress path is unenforceable.

    ``GraphBubbleUp`` propagates through ``LLMErrorHandlingMiddleware``
    (re-raised, not converted to a recoverable message), so the run actually
    fails instead of emitting a chat error.
    """


@dataclass(frozen=True)
class ChannelDecision:
    allowed: bool
    channel: str
    reason: str = ""


@dataclass(frozen=True)
class RunPolicy:
    tenant_id: str
    mode: str
    sensitive: bool


@dataclass(frozen=True)
class EgressConfig:
    default_mode: str = LOCAL_ONLY
    tenant_modes: Mapping[str, str] = field(default_factory=dict)
    tenant_classes: Mapping[str, str] = field(default_factory=dict)
    local_model_endpoints: tuple[str, ...] = ()
    allowed_model_endpoints: tuple[str, ...] = ()
    approved_model_endpoints: tuple[str, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()


def _env_str(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_list(name: str) -> tuple[str, ...] | None:
    raw = _env_str(name)
    if raw is None:
        return None
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _env_map(name: str) -> dict[str, str] | None:
    raw = _env_str(name)
    if raw is None:
        return None
    out: dict[str, str] = {}
    for pair in raw.split(","):
        key, sep, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if sep and key and value:
            out[key] = value
    return out


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a pydantic-style object or a plain dict."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _normalize_host_list(values: Sequence[str]) -> tuple[str, ...]:
    out: list[str] = []
    for raw in values:
        value = str(raw).strip().lower().rstrip(".")
        if value:
            out.append(value)
    return tuple(out)


def _host_in_list(host: str, allowed: Sequence[str]) -> bool:
    """Exact match or ``*.example.com`` wildcard covering proper subdomains."""
    host = host.lower().rstrip(".")
    for entry in allowed:
        if entry.startswith("*."):
            suffix = entry[2:]
            if host.endswith("." + suffix):
                return True
        elif host == entry:
            return True
    return False


def _is_local_host(host: str) -> bool:
    """True when the host provably cannot reach the public internet."""
    host = host.lower().rstrip(".")
    if host in {"localhost"} or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        # A DNS name could resolve to a private or public address — unprovable
        # without lookup, so it is not local under fail-closed semantics.
        return False
    return not addr.is_global


def _model_endpoint_host(model: Any) -> str | None:
    """Best-effort provider endpoint hostname extraction from a chat model."""
    candidates: list[Any] = [model]
    for attr in _MODEL_ENDPOINT_ATTRS:
        raw = getattr(model, attr, None)
        if raw:
            candidates.append(raw)
    for client_attr in ("client", "async_client"):
        client = getattr(model, client_attr, None)
        if client is not None:
            for attr in _MODEL_ENDPOINT_ATTRS:
                raw = getattr(client, attr, None)
                if raw:
                    candidates.append(raw)
    model_kwargs = getattr(model, "model_kwargs", None)
    if isinstance(model_kwargs, Mapping):
        for attr in ("base_url", "api_base", "openai_api_base"):
            raw = model_kwargs.get(attr)
            if raw:
                candidates.append(raw)

    for raw in candidates:
        if raw is model:
            continue
        text = str(raw).strip()
        if not text:
            continue
        if "://" not in text:
            text = "http://" + text
        host = urlparse(text).hostname
        if host:
            return host.lower()
    return None


def _tool_destination_host(args: Mapping[str, Any]) -> str | None:
    for key in _URL_ARG_KEYS:
        raw = args.get(key)
        if not raw:
            continue
        text = str(raw).strip()
        if "://" not in text:
            continue
        host = urlparse(text).hostname
        if host:
            return host.lower()
    return None


def _deny_marker(message: Any) -> dict[str, Any] | None:
    """Extract the structured denial marker from an ``AIMessage``."""
    kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(kwargs, Mapping):
        marker = kwargs.get(_DENY_KEY)
        if isinstance(marker, dict):
            return marker
    return None


class EgressPolicy:
    """Resolves and enforces per-tenant/run egress modes on every channel."""

    def __init__(self, config: EgressConfig) -> None:
        self.config = config

    @classmethod
    def resolve_config(cls, plugin_config: Mapping[str, Any]) -> EgressConfig:
        """Merge the plugin ``egress:`` block with env overrides (env wins)."""
        block = _get(plugin_config, "egress", {}) or {}

        default_mode = _env_str(_ENV_DEFAULT_MODE) or str(_get(block, "default_mode", LOCAL_ONLY))
        if default_mode not in _MODES:
            raise ValueError(f"ewcp egress: unknown default_mode {default_mode!r}")

        tenant_modes: dict[str, str] = dict(_get(block, "tenant_modes", {}) or {})
        for tenant, mode in list(tenant_modes.items()):
            if mode not in _MODES:
                raise ValueError(f"ewcp egress: unknown mode {mode!r} for tenant {tenant!r}")
        env_modes = _env_map(_ENV_TENANT_MODES)
        if env_modes is not None:
            for tenant, mode in env_modes.items():
                if mode not in _MODES:
                    raise ValueError(f"ewcp egress: unknown env mode {mode!r} for tenant {tenant!r}")
            tenant_modes.update(env_modes)

        tenant_classes: dict[str, str] = {tenant: _CLASS_NON_SENSITIVE for tenant in _BUILTIN_NON_SENSITIVE_TENANTS}
        for tenant, klass in dict(_get(block, "tenant_classes", {}) or {}).items():
            if klass not in _CLASSES:
                raise ValueError(f"ewcp egress: unknown data class {klass!r} for tenant {tenant!r}")
            tenant_classes[tenant] = klass
        env_classes = _env_map(_ENV_TENANT_CLASSES)
        if env_classes is not None:
            for tenant, klass in env_classes.items():
                if klass not in _CLASSES:
                    raise ValueError(f"ewcp egress: unknown env data class {klass!r} for tenant {tenant!r}")
                tenant_classes[tenant] = klass

        def _list(key: str, env_name: str) -> tuple[str, ...]:
            env = _env_list(env_name)
            values = env if env is not None else (_get(block, key, ()) or ())
            if env_name == _ENV_ALLOWED_TOOLS:
                return tuple(str(v).strip() for v in values if str(v).strip())
            return _normalize_host_list(values)

        return EgressConfig(
            default_mode=default_mode,
            tenant_modes=tenant_modes,
            tenant_classes=tenant_classes,
            local_model_endpoints=_list("local_model_endpoints", _ENV_LOCAL_ENDPOINTS),
            allowed_model_endpoints=_list("allowed_model_endpoints", _ENV_ALLOWED_MODEL_ENDPOINTS),
            approved_model_endpoints=_list("approved_model_endpoints", _ENV_APPROVED_MODEL_ENDPOINTS),
            allowed_domains=_list("allowed_domains", _ENV_ALLOWED_DOMAINS),
            allowed_tools=_list("allowed_tools", _ENV_ALLOWED_TOOLS),
        )

    # -- resolution ----------------------------------------------------------

    def resolve(self, context: Mapping[str, Any]) -> RunPolicy:
        tenant_id = str(context.get(_CTX_TENANT_ID) or context.get("user_id") or "")
        klass = self.config.tenant_classes.get(tenant_id, _CLASS_SENSITIVE)
        sensitive = klass != _CLASS_NON_SENSITIVE

        mode_override = context.get(_CTX_EGRESS_MODE)
        if isinstance(mode_override, str) and mode_override in _MODES:
            mode = mode_override
        else:
            mode = self.config.tenant_modes.get(tenant_id, self.config.default_mode)
        return RunPolicy(tenant_id=tenant_id, mode=mode, sensitive=sensitive)

    # -- channel checks ------------------------------------------------------

    def check_model_call(self, model: Any, context: Mapping[str, Any]) -> ChannelDecision:
        policy = self.resolve(context)
        if not policy.sensitive:
            return ChannelDecision(True, CHANNEL_MODEL)

        host = _model_endpoint_host(model)
        if host is None:
            return ChannelDecision(
                False,
                CHANNEL_MODEL,
                f"tenant {policy.tenant_id!r}: model endpoint unprovable under {policy.mode} — denied (fail-closed)",
            )

        if policy.mode == LOCAL_ONLY:
            if _is_local_host(host) or _host_in_list(host, self.config.local_model_endpoints):
                return ChannelDecision(True, CHANNEL_MODEL)
            return ChannelDecision(False, CHANNEL_MODEL, f"tenant {policy.tenant_id!r}: model endpoint {host!r} is not in-boundary (local_only)")
        if policy.mode == RESTRICTED:
            if _host_in_list(host, self.config.allowed_model_endpoints):
                return ChannelDecision(True, CHANNEL_MODEL)
            return ChannelDecision(
                False,
                CHANNEL_MODEL,
                f"tenant {policy.tenant_id!r}: model endpoint {host!r} not in allowed_model_endpoints (restricted)",
            )
        # approved_cloud
        if _host_in_list(host, self.config.approved_model_endpoints):
            return ChannelDecision(True, CHANNEL_MODEL)
        return ChannelDecision(
            False,
            CHANNEL_MODEL,
            f"tenant {policy.tenant_id!r}: model endpoint {host!r} not in approved_model_endpoints (approved_cloud)",
        )

    def check_sandbox_net(self, context: Mapping[str, Any]) -> ChannelDecision:
        """Capability gate: is the configured sandbox provider's network
        isolation sufficient for this run's mode?

        This is the seam the plan requires — middleware cannot isolate the
        network; the provider either can (AIO isolated/allowlist) or has no
        egress path at all (local provider, host bash disabled). Anything
        else is unenforceable for local_only/restricted.
        """
        policy = self.resolve(context)
        if not policy.sensitive or policy.mode == APPROVED_CLOUD:
            return ChannelDecision(True, CHANNEL_SANDBOX_NET)

        app_config = context.get("app_config")
        sandbox = _get(app_config, "sandbox") if app_config is not None else None
        if sandbox is None:
            return ChannelDecision(False, CHANNEL_SANDBOX_NET, "sandbox configuration unprovable — denied (fail-closed)")

        use = str(_get(sandbox, "use", "") or "")
        allow_host_bash = bool(_get(sandbox, "allow_host_bash", False))
        provider = use.rsplit(":", 1)[-1].rsplit(".", 1)[-1]

        if provider == "LocalSandboxProvider":
            if allow_host_bash:
                verdict = "unenforceable: host bash reaches the host network"
            else:
                verdict = "no_egress_path"
        elif provider == "AioSandboxProvider":
            network = _get(sandbox, "network")
            mode = str(_get(network, "mode", "open") or "open")
            if mode == "isolated":
                verdict = "isolated"
            elif mode == "allowlist":
                approval = str(_get(network, "approval", "prompt") or "prompt")
                verdict = f"allowlist(approval={approval})"
            else:
                verdict = "open"
        else:
            verdict = f"unmanaged:{provider or 'unknown'}"

        if policy.mode == LOCAL_ONLY:
            ok = verdict in {"isolated", "no_egress_path"}
        else:  # restricted
            ok = verdict in {"isolated", "no_egress_path", "allowlist(approval=deny)"}
        if ok:
            return ChannelDecision(True, CHANNEL_SANDBOX_NET)
        return ChannelDecision(
            False,
            CHANNEL_SANDBOX_NET,
            f"tenant {policy.tenant_id!r}: sandbox-net verdict {verdict!r} insufficient for {policy.mode} — denied (fail-closed)",
        )

    def check_tool_call(self, tool_name: str, args: Mapping[str, Any], context: Mapping[str, Any]) -> ChannelDecision:
        policy = self.resolve(context)
        if not policy.sensitive:
            return ChannelDecision(True, CHANNEL_TOOL)
        name = str(tool_name or "")

        if policy.mode == APPROVED_CLOUD:
            return ChannelDecision(True, CHANNEL_TOOL)

        if policy.mode == LOCAL_ONLY:
            if name in _SAFE_BUILTIN_TOOLS:
                return ChannelDecision(True, CHANNEL_TOOL)
            if name in _FILESYSTEM_TOOLS:
                if name == "bash":
                    sandbox = self.check_sandbox_net(context)
                    if not sandbox.allowed:
                        return ChannelDecision(False, CHANNEL_TOOL, f"bash rides sandbox egress: {sandbox.reason}")
                return ChannelDecision(True, CHANNEL_TOOL)
            return ChannelDecision(False, CHANNEL_TOOL, f"tool {name!r} egress-capable or unknown under local_only — denied")

        # restricted
        if name in self.config.allowed_tools:
            return ChannelDecision(True, CHANNEL_TOOL)
        if name in _SAFE_BUILTIN_TOOLS:
            return ChannelDecision(True, CHANNEL_TOOL)
        if name in _FILESYSTEM_TOOLS:
            if name == "bash":
                sandbox = self.check_sandbox_net(context)
                if not sandbox.allowed:
                    return ChannelDecision(False, CHANNEL_TOOL, f"bash rides sandbox egress: {sandbox.reason}")
            return ChannelDecision(True, CHANNEL_TOOL)
        if name in _NETWORK_TOOLS or name.startswith(_NETWORK_TOOL_PREFIXES):
            host = _tool_destination_host(args)
            if host is not None and _host_in_list(host, self.config.allowed_domains):
                return ChannelDecision(True, CHANNEL_TOOL)
            return ChannelDecision(
                False,
                CHANNEL_TOOL,
                f"tool {name!r} destination not in allowed_domains under restricted — denied",
            )
        return ChannelDecision(False, CHANNEL_TOOL, f"tool {name!r} egress-capable or unknown under restricted — denied")

    # -- run admission -------------------------------------------------------

    def enforce_run(self, context: Mapping[str, Any]) -> None:
        """Fail-closed admission gate: a sensitive workload must not start
        while a mandatory data path (sandbox-net) is unenforceable."""
        sandbox = self.check_sandbox_net(context)
        if not sandbox.allowed:
            raise EgressDeniedError(f"ewcp egress policy: {sandbox.reason}")


# --- middleware adapters -----------------------------------------------------


def _denial_message(decision: ChannelDecision) -> AIMessage:
    return AIMessage(
        content=(f"EWCP egress policy denied this model call. channel={decision.channel} reason={decision.reason}"),
        additional_kwargs={_DENY_KEY: {"channel": decision.channel, "reason": decision.reason}},
    )


def _denial_tool_message(tool_call: Mapping[str, Any], decision: ChannelDecision) -> ToolMessage:
    name = tool_call.get("name", "?")
    return ToolMessage(
        content=(f"EWCP egress policy denied tool {name!r}. channel={decision.channel} reason={decision.reason}"),
        tool_call_id=str(tool_call.get("id", "")),
        status="error",
    )


class EgressPolicyMiddleware(AgentMiddleware):
    """MODEL_PHYSICAL hook: per-call endpoint allow/deny + admission gate."""

    def __init__(self, policy: EgressPolicy) -> None:
        super().__init__()
        self.policy = policy

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        context = getattr(runtime, "context", None) or {}
        self.policy.enforce_run(context)
        return None

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        context = getattr(getattr(request, "runtime", None), "context", None) or {}
        decision = self.policy.check_model_call(getattr(request, "model", None), context)
        if not decision.allowed:
            return _denial_message(decision)
        return await handler(request)


class EgressToolMiddleware(AgentMiddleware):
    """TOOL_RAW hook: allow/deny adjacent to the real callable."""

    def __init__(self, policy: EgressPolicy) -> None:
        super().__init__()
        self.policy = policy

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        context = getattr(getattr(request, "runtime", None), "context", None) or {}
        tool_call = getattr(request, "tool_call", None) or {}
        decision = self.policy.check_tool_call(tool_call.get("name"), tool_call.get("args") or {}, context)
        if not decision.allowed:
            return _denial_tool_message(tool_call, decision)
        return await handler(request)


class EgressMiddlewareContributor:
    """Contributes the two egress hooks at their required placements."""

    def __init__(self, policy: EgressPolicy) -> None:
        self.policy = policy

    def contribute_middlewares(self, app_store: Any, ctx: Any) -> Sequence[MiddlewarePlacement]:
        return (
            MiddlewarePlacement(
                middleware=EgressPolicyMiddleware(self.policy),
                placement=Placement.MODEL_PHYSICAL,
                scope=AgentScope.BOTH,
            ),
            MiddlewarePlacement(
                middleware=EgressToolMiddleware(self.policy),
                placement=Placement.TOOL_RAW,
                scope=AgentScope.BOTH,
            ),
        )
