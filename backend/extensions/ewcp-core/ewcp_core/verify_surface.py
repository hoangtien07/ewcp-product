"""PUBLIC verify surface — the kernel's anonymous verify contract.

`GET /verify/{manifest_hash}` (seal permalink) and `POST /verify`
(verify-by-them upload) serve anonymous third parties: the manifest hash
itself is the capability, same contract as the kernel's public verify
routes. They never consult request.state.user.

These handlers are mounted by HOST routes
(`app.gateway.ewcp_verify`) — not by the extension's contributed router:
the harness fences every extension route claim out of the host public
namespace, so anonymous paths can only be mounted host-side. The host
module locates the running service through `app.state.extensions` here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile

from .api_routes import _kernel_error, _require_client

if TYPE_CHECKING:
    from .plugin import EwcpCoreService


def resolve_verify_service(request: Request) -> EwcpCoreService:
    """Locate the running EwcpCoreService on the host app.

    Host-mounted routes get no router closure, so the service is found
    on `app.state.extensions` (the LoadedExtensions the gateway records
    at startup) instead of being captured at router build time.
    """
    from .plugin import EwcpCoreService

    loaded = getattr(request.app.state, "extensions", None)
    services = getattr(loaded, "services", ()) if loaded is not None else ()
    for _source, service in services:
        if isinstance(service, EwcpCoreService):
            return service
    raise HTTPException(404, "ewcp-core extension is not installed")


async def verify_permalink(request: Request, manifest_hash: str) -> dict[str, Any]:
    """PUBLIC seal permalink — `GET /verify/{manifest_hash}`.

    Anonymous third parties resolve seal state + manifest contents
    through it; the manifest hash is the capability.
    """
    client = _require_client(resolve_verify_service(request))
    try:
        return await client.verify_manifest(manifest_hash)
    except Exception as exc:
        raise _kernel_error(exc) from exc


async def verify_evidence(request: Request) -> dict[str, Any]:
    """PUBLIC verify-by-them upload — `POST /verify`.

    Byte-integrity check: forward the verifier's evidence.json +
    declared artifacts to the kernel's `POST /verify` (same anonymous
    contract kernel-side).
    """
    client = _require_client(resolve_verify_service(request))
    form = await request.form()
    evidence = form.get("evidence_json")
    if not isinstance(evidence, UploadFile):
        raise HTTPException(422, "evidence_json file is required")
    files = [(f.filename or "file", await f.read()) for f in form.getlist("files") if isinstance(f, UploadFile)]
    try:
        return await client.verify_evidence(
            evidence_json=(evidence.filename or "evidence.json", await evidence.read()),
            files=files,
        )
    except Exception as exc:
        raise _kernel_error(exc) from exc
