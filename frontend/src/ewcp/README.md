# frontend/src/ewcp — EWCP UI surfaces

All EWCP-specific frontend code lives in this directory (boxed; no imports
from here into upstream components — upstream may import FROM here via thin
adapters recorded in UPSTREAM_TOUCH.md).

Planned surfaces (workspace-council studio archaeology — commodity only):

- `TaskThread` — governed task thread: intent → ask-back cards → progress →
  decision card → sealed manifest card.
- `ManifestCard` — VerificationManifest render (sealed hash, checks,
  decided_by) + link to verify + `ShareVerifyLink` (copies the public
  `/ewcp/verify?manifest=<hash>` permalink for third parties).
- `VerifyView` — public surfaces: permalink lookup (`?manifest=` →
  GET /verify/{hash}, no auth — the hash is the capability) + re-verify
  upload result (PASS/FAIL + failing file name).
- `UnverifiedBadge` — badge dominating all artifact chrome on exploratory
  (non-governed) artifacts; provenance ≠ verification (Perplexity trap).
- `DossierViewer` — doc-viewer + form panel for chứng từ dossier (pdf.js).
- `ReconGrid` — đối soát grid (rows = chứng từ, cols = checks, per-cell
  citation + status + user-override-wins) — Harvey/Hebbia-lite pattern.

API: all calls go to `/api/ewcp/*` (extension router → in-process kernel).
