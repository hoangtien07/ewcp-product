"""Agent-facing EWCP capability tools — WP-A5a Wave-2 consumer binding
(plan `docs/program/plans/2026-10-09-A5a-capability-contract.md` §5).

One tool, not per-pack tools: `ewcp_invoke` calls the kernel's typed
capability contract (`POST /outcomes/{type}/run`, contract v1) and
`ewcp_capabilities` lists the `GET /outcomes` descriptors so the agent can
pick by capability_id / side_effect_class / input schema.

Forbid tool bypass: both tools travel through the extension's KernelClient
(tenant key + actor binding already configured on the service); the agent
never receives the raw tenant key or a generic HTTP tool for kernel
routes. Egress for kernel-bound tools is checked against the configured
kernel_url destination in egress_policy (`_KERNEL_BOUND_TOOLS`).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import httpx
from deerflow.config.paths import get_paths
from deerflow.runtime.user_context import get_effective_user_id
from deerflow.tools.types import Runtime
from deerflow_extension_api.placement import AgentScope, MiddlewarePlacement, Placement
from langchain.agents.middleware import AgentMiddleware
from langchain.tools import tool
from sqlalchemy.exc import IntegrityError

from .execution_run_store import ExecutionRunRecord
from .kernel_client import InvokeResult, KernelClient, KernelInvokeError
from .run_launcher import ExecutionRunStatus

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 100 * 1024 * 1024

#: task_mode value on ExecutionRunMap rows minted by ewcp_invoke (not a
#: launcher TaskMode — the pane cannot launch one). Marks the row as an
#: invocation record: `_execution_run_id` never selects it as the run's own
#: identity row, and the pane can tell chat-invoked work apart.
INVOKE_TASK_MODE = "invoke"
# Directories (under the thread's user-data root) that `files:` names may
# resolve against. Bare names default to uploads/.
_FILE_DIRS = ("uploads", "workspace", "outputs")


def _none() -> None:
    return None


@dataclass
class InvokeDeps:
    """Late-bound service seams. The KernelClient/ExecutionRunStore only
    exist after `service.start()`, which may run after the agent is built —
    hence getters, not instances."""

    client_getter: Callable[[], KernelClient | None]
    store_getter: Callable[[], Any] = _none
    tenant_id_getter: Callable[[], str | None] = _none
    paths_getter: Callable[[], Any] = get_paths
    # Contract descriptors are process-stable (contract_version is frozen
    # server-side), so they are cached for the middleware's lifetime.
    descriptor_cache: dict[str, dict[str, Any]] = field(default_factory=dict)


def _result(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _fail(error: str, message: str, guidance: str, **extra: Any) -> str:
    return _result({"ok": False, "error": error, "message": message, "guidance": guidance, **{k: v for k, v in extra.items() if v is not None}})


def _invoke_error_payload(exc: KernelInvokeError, *, hint: str | None = None) -> str:
    """Map a structured kernel rejection to a readable tool error —
    preserves the contract's `error` code + `message`; the 409 pair is
    split by guidance: payload mismatch is retryable-with-correction,
    unsupported_version is fatal."""
    hints = {
        "idempotency_payload_mismatch": "that Idempotency-Key was already used with a different payload — resend the ORIGINAL payload, or omit idempotency_key to mint a fresh key",
        "unsupported_version": "the kernel does not speak contract_version=1 — do not retry; escalate deployment drift",
        "missing_required_input": "supply the fields listed in `missing`, then retry",
        "schema_invalid": "fix the payload against the capability's input_schema, then retry",
        "unknown_capability": "call ewcp_capabilities to list valid capability_ids/outcome_types",
        "unauthenticated": "kernel authentication failed — deployment config issue, do not retry",
        "tenant_mismatch": "declared tenant does not match the API key tenant — deployment config issue",
        "payload_too_large": "reduce the upload size/count, then retry",
        "conflict": "the invocation conflicts with existing kernel state — inspect and retry only with corrected inputs",
    }
    return _result(
        {
            "ok": False,
            "error": exc.error,
            "message": exc.message,
            "status": exc.status,
            "guidance": exc.guidance,
            "hint": hint or hints.get(exc.error),
            "extra": exc.extra or None,
        }
    )


def _capability_summary(item: Mapping[str, Any]) -> dict[str, Any]:
    """Compact descriptor view for the listing tool — capability block
    fields per contract §descriptor surface, tolerating pre-A5a kernels
    where the `capability` block is absent."""
    cap = item.get("capability") if isinstance(item.get("capability"), Mapping) else item
    schema = cap.get("input_schema") if isinstance(cap.get("input_schema"), Mapping) else {}
    inputs = [{k: slot.get(k) for k in ("name", "required", "accept", "multiple") if k in slot} for slot in schema.get("inputs") or [] if isinstance(slot, Mapping)]
    context = [{k: f.get(k) for k in ("name", "type", "required", "default") if k in f} for f in schema.get("context") or [] if isinstance(f, Mapping)]
    return {
        "capability_id": cap.get("capability_id"),
        "outcome_type": cap.get("outcome_type") or item.get("outcome_type"),
        "contract_version": cap.get("contract_version"),
        "summary": cap.get("summary"),
        "when_to_use": cap.get("when_to_use"),
        "side_effect_class": cap.get("side_effect_class"),
        "inputs": inputs,
        "context": context,
        "required_context": schema.get("required_context") or cap.get("required_context"),
    }


async def _execution_run_id(deps: InvokeDeps, ctx: Mapping[str, Any]) -> str | None:
    """Bind the invocation to the caller's execution run (contract field
    `execution_run_id`) so the kernel budget ledger joins it with the
    run's model/budget admissions — the governed workrun first (context
    stamp left by the egress stamp pass; budget admission resolves the
    same binding from the ExecutionRunMap directly), then the product
    ExecutionRun record for this thread/run, then the raw run_id."""
    kernel_ctx = ctx.get("kernel")
    if isinstance(kernel_ctx, Mapping) and kernel_ctx.get("workrun_id"):
        return str(kernel_ctx["workrun_id"])
    store = deps.store_getter()
    thread_id = ctx.get("thread_id")
    run_id = ctx.get("run_id")
    if store is not None and thread_id:
        try:
            records = await store.list_for_thread(str(thread_id))
        except Exception:  # noqa: BLE001 — store failure must not block the invoke
            logger.warning("execution-run store lookup failed for thread %s", thread_id, exc_info=True)
        else:
            # Invocation records (task_mode=invoke) belong to a capability
            # call, not to the calling run — picking one here would join a
            # later invoke's budget onto an earlier invocation's account.
            candidates = [r for r in records if getattr(r, "task_mode", None) != INVOKE_TASK_MODE]
            for record in candidates:
                if run_id and record.run_id == run_id:
                    return record.execution_run_id
            if candidates:
                return candidates[0].execution_run_id
    return str(run_id) if run_id else None


async def _bind_invocation_record(
    deps: InvokeDeps,
    ctx: Mapping[str, Any],
    *,
    outcome_type: str,
    idempotency_key: str,
    run: Mapping[str, Any],
) -> str | None:
    """Project a successful invoke into the ExecutionRunMap (A6 #13):
    the hybrid lane's deep link `/workspace/ewcp-runs/{id}` resolves only
    when a map row binds the kernel workrun to this thread/run. Returns
    the row's execution_run_id — None when the store, thread, or workrun
    is absent (the invoke already succeeded kernel-side; a projection
    failure must not downgrade the tool result)."""
    workrun_id = run.get("workrun_id")
    store = deps.store_getter()
    thread_id = ctx.get("thread_id")
    if store is None or not thread_id or not workrun_id:
        return None
    created_by = str(ctx.get("user_id") or get_effective_user_id())
    try:
        existing = await store.get_by_idempotency_key(created_by, idempotency_key)
        if existing is not None:
            if not existing.workrun_id:
                await store.bind_workrun(existing.execution_run_id, str(workrun_id))
            elif existing.workrun_id != str(workrun_id):
                logger.warning(
                    "invoke map row %s already bound to %s, kernel returned %s — keeping the first binding",
                    existing.execution_run_id,
                    existing.workrun_id,
                    workrun_id,
                )
            return existing.execution_run_id
        run_id = ctx.get("run_id")
        record = await store.insert(
            ExecutionRunRecord.new(
                thread_id=str(thread_id),
                run_id=str(run_id) if run_id else None,
                workrun_id=str(workrun_id),
                task_mode=INVOKE_TASK_MODE,
                status=ExecutionRunStatus.RUNNING.value,
                intent=f"ewcp_invoke:{outcome_type}",
                idempotency_key=idempotency_key,
                created_by=created_by,
            )
        )
        return record.execution_run_id
    except IntegrityError:
        # A concurrent invoke replayed the same idempotency key and won
        # the insert — converge on its row instead of duplicating it.
        try:
            raced = await store.get_by_idempotency_key(created_by, idempotency_key)
        except Exception:  # noqa: BLE001
            return None
        return raced.execution_run_id if raced is not None else None
    except Exception:  # noqa: BLE001
        logger.warning("invoke ExecutionRunMap projection failed for thread %s", thread_id, exc_info=True)
        return None


def _resolve_upload_path(root: Path, name: str) -> Path | None:
    """Resolve an agent-supplied file name under the thread's user-data
    root. Accepts bare names (uploads/) or `uploads|workspace|outputs/…`
    and `/mnt/user-data/…` virtual prefixes; rejects traversal outside
    the root."""
    raw = str(name or "").strip().lstrip("/")
    if raw.startswith("mnt/user-data/"):
        raw = raw[len("mnt/user-data/") :]
    parts = raw.split("/", 1)
    if parts[0] in _FILE_DIRS and len(parts) == 2:
        subdir, rest = parts
    else:
        subdir, rest = "uploads", raw
    if not rest or ".." in rest.split("/"):
        return None
    candidate = (root / subdir / rest).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _resolve_files(
    deps: InvokeDeps,
    ctx: Mapping[str, Any],
    files: Mapping[str, Sequence[str]],
) -> tuple[dict[str, list[tuple]], str | None]:
    """Map `files={slot: [names]}` to upload tuples — bytes read from the
    thread's user-data dirs (uploads first for bare names)."""
    if not files:
        return {}, None
    thread_id = ctx.get("thread_id")
    if not thread_id:
        return {}, _fail("invalid_request", "files= requires a thread-bound run (no thread_id in runtime context)", "correct")
    user_id = str(ctx.get("user_id") or get_effective_user_id())
    # Read bytes via the gateway-local namespace — the same one the upload
    # write path (sandbox_uploads_dir) and list_uploaded_files use. host_* is
    # the docker-daemon mount-source namespace (DEER_FLOW_HOST_BASE_DIR) and is
    # not readable in-process on provisioner/DooD deployments.
    root = Path(deps.paths_getter().sandbox_user_data_dir(str(thread_id), user_id=user_id))
    out: dict[str, list[tuple]] = {}
    for slot, names in files.items():
        if not isinstance(names, (list, tuple)):
            return {}, _fail("invalid_request", f"files[{slot!r}] must be a list of filenames", "correct")
        for name in names:
            path = _resolve_upload_path(root, str(name))
            if path is None or not path.is_file():
                return {}, _fail(
                    "invalid_request",
                    f"file {name!r} not found under this thread's uploads/workspace/outputs — call list_uploaded_files or produce the file first",
                    "correct",
                )
            size = path.stat().st_size
            if size > _MAX_FILE_BYTES:
                return {}, _fail("payload_too_large", f"file {name!r} is {size} bytes (cap {_MAX_FILE_BYTES})", "correct")
            out.setdefault(str(slot), []).append((path.name, path.read_bytes(), None))
    return out, None


async def capabilities_impl(deps: InvokeDeps, ctx: Mapping[str, Any], *, outcome_type: str | None = None) -> str:
    client = deps.client_getter()
    if client is None:
        return _fail("kernel_not_configured", "EWCP kernel is not configured (kernel_url missing) — capability tools are unavailable", "fatal")
    try:
        if outcome_type:
            descriptor = await client.get_outcome_descriptor(outcome_type)
            return _result({"ok": True, "descriptor": descriptor})
        items = await client.list_outcomes()
    except KernelInvokeError as exc:
        return _invoke_error_payload(exc)
    except httpx.HTTPError as exc:
        return _fail("kernel_unreachable", f"kernel request failed: {exc.__class__.__name__}", "retry")
    return _result({"ok": True, "capabilities": [_capability_summary(item) for item in items]})


async def invoke_impl(
    deps: InvokeDeps,
    ctx: Mapping[str, Any],
    *,
    outcome_type: str,
    context: Mapping[str, Any] | None = None,
    files: Mapping[str, Sequence[str]] | None = None,
    idempotency_key: str | None = None,
) -> str:
    client = deps.client_getter()
    if client is None:
        return _fail("kernel_not_configured", "EWCP kernel is not configured (kernel_url missing) — ewcp_invoke is unavailable", "fatal")

    # Contract parsing is NOT stubbed: fetch the descriptor (cached) and
    # validate required context + declared file slots before the POST.
    try:
        descriptor = deps.descriptor_cache.get(outcome_type) or await client.get_outcome_descriptor(outcome_type)
        deps.descriptor_cache[outcome_type] = descriptor
    except KernelInvokeError as exc:
        return _invoke_error_payload(exc)
    except httpx.HTTPError as exc:
        return _fail("kernel_unreachable", f"kernel request failed: {exc.__class__.__name__}", "retry")

    schema = descriptor.get("input_schema") if isinstance(descriptor.get("input_schema"), Mapping) else {}
    context = dict(context or {})
    required = schema.get("required_context") or []
    missing = [name for name in required if context.get(name) in (None, "")]
    if missing:
        return _fail(
            "missing_required_input",
            f"capability {outcome_type!r} requires context field(s): {', '.join(map(str, missing))}",
            "correct",
            missing=list(missing),
            hint="supply the missing context fields, then retry",
        )
    declared_slots = {str(slot.get("name")) for slot in schema.get("inputs") or [] if isinstance(slot, Mapping)}
    files = dict(files or {})
    unknown_slots = [slot for slot in files if str(slot) not in declared_slots]
    if unknown_slots:
        return _fail(
            "invalid_request",
            f"unknown file slot(s) {unknown_slots} — declared inputs: {sorted(declared_slots) or 'none'}",
            "correct",
        )
    resolved_files, file_error = _resolve_files(deps, ctx, files)
    if file_error is not None:
        return file_error

    # Fresh correlation ids per logical invocation; client-side retries
    # inside KernelClient reuse the SAME Idempotency-Key and payload.
    key = str(idempotency_key) if idempotency_key else f"invoke-{uuid.uuid4().hex}"
    invocation_id = f"inv-{uuid.uuid4().hex}"
    execution_run_id = await _execution_run_id(deps, ctx)

    try:
        result: InvokeResult = await client.invoke_outcome(
            outcome_type,
            context=context,
            files=resolved_files,
            tenant_id=deps.tenant_id_getter(),
            invocation_id=invocation_id,
            execution_run_id=execution_run_id,
            idempotency_key=key,
        )
    except KernelInvokeError as exc:
        return _invoke_error_payload(exc)
    except httpx.HTTPError as exc:
        return _fail("kernel_unreachable", f"kernel request failed: {exc.__class__.__name__}", "retry")
    except RuntimeError as exc:
        return _fail("kernel_unreachable", str(exc), "retry")

    run = result.run
    map_id = await _bind_invocation_record(
        deps,
        ctx,
        outcome_type=outcome_type,
        idempotency_key=key,
        run=run,
    )
    return _result(
        {
            "ok": True,
            "capability_id": descriptor.get("capability_id") or f"pack:{outcome_type}",
            "invocation_id": run.get("invocation_id") or invocation_id,
            "execution_run_id": run.get("execution_run_id") or execution_run_id,
            "execution_run_map_id": map_id,
            "idempotency_key": key,
            "idempotent_replay": result.idempotent_replay,
            "run": run,
        }
    )


def _make_capabilities_tool(deps: InvokeDeps):
    @tool
    async def ewcp_capabilities(
        runtime: Runtime,
        outcome_type: Annotated[
            str | None,
            "Optional outcome_type (e.g. 'invoice_recon') to fetch that capability's full descriptor. Omit to list all capabilities.",
        ] = None,
    ) -> str:
        """List EWCP kernel capabilities — governed outcome packs callable via ewcp_invoke.

        Returns each capability's capability_id, outcome_type, summary, when_to_use,
        side_effect_class, declared input file slots, and context fields. Call this
        BEFORE ewcp_invoke to pick a capability and learn its required inputs."""
        return await capabilities_impl(deps, dict(getattr(runtime, "context", None) or {}), outcome_type=outcome_type)

    return ewcp_capabilities


def _make_invoke_tool(deps: InvokeDeps):
    @tool
    async def ewcp_invoke(
        runtime: Runtime,
        outcome_type: Annotated[
            str,
            "The capability's outcome_type (e.g. 'invoice_recon'). Get valid values from ewcp_capabilities.",
        ],
        context: Annotated[
            dict[str, Any] | None,
            "Typed context fields declared by the capability's input_schema.context (e.g. period, currency). Values are scalars or JSON.",
        ] = None,
        files: Annotated[
            dict[str, list[str]] | None,
            "Map of declared input slot -> filename(s) from this thread's uploads/workspace/outputs, e.g. {'invoices_zip': ['invoices.zip']}. Bare names resolve under uploads/.",
        ] = None,
        idempotency_key: Annotated[
            str | None,
            "Optional Idempotency-Key. Omit to mint a fresh key per call — reuse the SAME key ONLY to retry the identical logical invocation.",
        ] = None,
    ) -> str:
        """Invoke an EWCP capability on the governed kernel (contract v1: POST /outcomes/{type}/run).

        Returns the run view: status, workrun_id, deliverables, and the echoed
        invocation_id/execution_run_id correlation fields. Requires the capability's
        declared context fields and file inputs — see ewcp_capabilities for the schema."""
        return await invoke_impl(
            deps,
            dict(getattr(runtime, "context", None) or {}),
            outcome_type=outcome_type,
            context=context,
            files=files,
            idempotency_key=idempotency_key,
        )

    return ewcp_invoke


class EwcpInvokeToolsMiddleware(AgentMiddleware):
    """Declares the two kernel capability tools. No hooks — ordering is
    irrelevant, so it contributes at STANDARD. Tools are stateless
    closures over InvokeDeps; copy.copy (declared-tool-view narrowing)
    shares them safely."""

    def __init__(self, deps: InvokeDeps) -> None:
        super().__init__()
        self.tools = (_make_capabilities_tool(deps), _make_invoke_tool(deps))


class InvokeToolsContributor:
    """Contributes EwcpInvokeToolsMiddleware at STANDARD placement.
    Non-intercepting: a wrapped failure only degrades to the host's
    IsolatedMiddleware reporting — these tools never deny."""

    def __init__(self, deps_factory: Callable[[], InvokeDeps]) -> None:
        self._deps_factory = deps_factory

    def contribute_middlewares(self, app_store: Any, ctx: Any) -> Sequence[MiddlewarePlacement]:
        return (
            MiddlewarePlacement(
                middleware=EwcpInvokeToolsMiddleware(self._deps_factory()),
                placement=Placement.STANDARD,
                scope=AgentScope.BOTH,
                intercepting=False,
            ),
        )
