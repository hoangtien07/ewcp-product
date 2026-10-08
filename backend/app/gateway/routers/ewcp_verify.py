"""EWCP public seal-verify routes — anonymous by kernel contract.

`GET /api/ewcp/verify/{manifest_hash}` (seal permalink) and
`POST /api/ewcp/verify` (verify-by-them) mirror the EWCP kernel's public
verify routes: the manifest hash itself is the capability, so third-party
verifiers reach these without a session. Both paths sit in
`auth_middleware._PUBLIC_PATH_PREFIXES` and the POST path in
`csrf_middleware._CSRF_EXEMPT_EXACT_PATHS` (an anonymous upload has no
session to protect); the mirror sets in
`packages/harness/deerflow/extensions/gateway.py` track them.

These are HOST routes, not extension-contributed: the extension gateway
fences every contributed claim out of the host public namespace, so an
anonymous path can only be mounted here. The ewcp-core extension keeps
the kernel logic (`ewcp_core.verify_surface`); when the extension is not
installed the handlers 404 — the permalink surface exists only where the
extension does.
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["ewcp"])


def _verify_impl():
    try:
        from ewcp_core import verify_surface
    except ImportError as exc:
        raise HTTPException(404, "ewcp verify surface is not installed") from exc
    return verify_surface


@router.get("/api/ewcp/verify/{manifest_hash}")
async def verify_permalink(manifest_hash: str, request: Request) -> dict[str, Any]:
    return await _verify_impl().verify_permalink(request, manifest_hash)


@router.post("/api/ewcp/verify")
async def verify_evidence(request: Request) -> dict[str, Any]:
    return await _verify_impl().verify_evidence(request)
