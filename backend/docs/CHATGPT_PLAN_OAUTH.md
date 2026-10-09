# ChatGPT Plan OAuth provider (`chatgpt_plan_oauth`)

Official **Sign in with ChatGPT — ChatGPT Plan Usage** integration for DeerFlow.
It lets one *eligible* user run supported DeerFlow inference against their own
ChatGPT Plus/Pro subscription instead of an OpenAI Platform API key.

This is an **optional model provider**, not a change to the agent harness.
The existing Codex provider (`CodexChatModel` → `chatgpt.com/backend-api/codex/*`
+ Codex CLI credentials) is a different grant and is **not** interchangeable —
this provider never reads `~/.codex/auth.json` and never calls `backend-api`.

Source docs (all claims below cite these):

- https://developers.openai.com/siwc/quickstart
- https://developers.openai.com/siwc/token-sharing-open-source
- https://developers.openai.com/siwc/token-sharing-open-source/sign-in
- https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference
- https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions
- https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations
- https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms
- https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery

## Eligibility matrix (as documented today)

| Deployment mode | Status per docs | What this implementation does |
|---|---|---|
| Open-source **local** application (user runs on their own machine, completes OAuth locally) | ✅ Eligible — documented flow | Fully supported. This is the only mode enabled. |
| **Self-hosted** open-source app on a VM | ✅ Eligible via documented procedure — complete OAuth **locally**, then transfer the credential file to the VM over a secure channel | Supported via the `import` subcommand. The VM mints its own `ext_agent_host_id` and owns refreshes from that point. Doc caveat: host-specific attribution/revocation of transferred sessions is "not yet available". |
| Private **remotely hosted** web application | ⚠️ Not covered by the open-source flow — commercial/multi-user hosting is a "limited trial" gated by OpenAI's interest form | Disabled. Do not ship this provider in the hosted EWCP build until OpenAI approves. |
| **Commercial / multi-user SaaS** | ⚠️ Same limited-trial gate | Disabled, same reason. No shared org-wide credential is created anywhere in this code path. |

Other documented constraints:

- A successful `GET /v1/models` list is **not** proof of inference eligibility —
  validate with a real `POST /v1/responses` call.
- `chatgpt.tokens.use.direct` is the inference grant. Sign-in can succeed
  without it; the provider then refuses to send requests and tells the user to
  re-consent.
- ChatGPT **Plus** accounts share a 5-hour usage window across connected apps;
  **Pro** is exempt from that window. `subscription_sharing_usage_limit_exceeded`
  is non-retriable — pause and link the user to ChatGPT settings → Usage.
- Preview surface: no `temperature`/`top_p`/`max_output_tokens`/`metadata`/
  `previous_response_id`, no hosted tools (image gen, file search, Code
  Interpreter, computer use, hosted MCP/connectors). Function/custom tools
  only. `store=false`, `stream=true` are mandatory.

## Files

- `packages/harness/deerflow/models/chatgpt_plan_oauth.py` — OAuth layer + CLI
- `packages/harness/deerflow/models/chatgpt_plan_provider.py` — `ChatGPTPlanChatModel`
- `tests/test_chatgpt_plan_oauth.py`, `tests/test_chatgpt_plan_provider.py` — mocked suite
- `scripts/benchmark/chatgpt_plan_eval/` — DeerFlow-level benchmark harness

## Local authorization (founder runs this on his machine)

1. Prerequisites: `backend/.venv` exists (`cd backend && uv sync` if not).
   The browser dance runs on **your** machine — Devin/CI cannot and must not
   do it; do not paste ChatGPT credentials anywhere.
2. Start the flow:
   ```bash
   cd backend
   .venv/bin/python -m deerflow.models.chatgpt_plan_oauth login
   ```
   This binds a `http://127.0.0.1:<port>/auth/callback` listener (default port
   1455, ephemeral fallback), prints the authorize URL, and opens your browser.
   `--no-browser` prints only. `--port`, `--timeout`, `--account` selectors exist.
3. In the browser: sign in with ChatGPT and approve the consent screen. The
   callback lands on `127.0.0.1`, the code is exchanged for tokens, the ID
   token is verified against OpenAI's JWKS (signature, issuer, audience,
   expiry, nonce), and the credential record is written atomically with `0600`
   permissions to `<runtime_home>/chatgpt-plan/accounts/<client_id>.json`
   (override: `CHATGPT_PLAN_CREDENTIALS_DIR`).
4. If consent was granted **without** `chatgpt.tokens.use.direct`, the CLI warns
   that plan usage is disabled — re-run `login --account <email>` (reauth reuses
   the issued `client_id`) and check the consent screen's scope grant.
5. Verify the grant + discover models:
   ```bash
   .venv/bin/python -m deerflow.models.chatgpt_plan_oauth accounts   # redacted view
   .venv/bin/python -m deerflow.models.chatgpt_plan_oauth models     # requires scope
   ```
   Pick a `slug` from the catalog for `model:` in `config.yaml`.
6. Minimal live inference proof (required before claiming integration works):
   a small real `POST /v1/responses` call — a successful *model list alone is
   not evidence*. The benchmark harness's `--smoke` mode does exactly this and
   prints latency/usage so the account's live path is proven end-to-end.

## Self-hosted VM procedure (authorized path)

1. Complete `login` on a machine you control (e.g. laptop).
2. Transfer `accounts/<client_id>.json` + the `host.json` **is NOT copied** —
   the VM keeps its own host identity. Copy only the account record file via a
   secure channel (scp/encrypted disk), then on the VM:
   ```bash
   .venv/bin/python -m deerflow.models.chatgpt_plan_oauth import /path/to/<client_id>.json
   ```
   The record is re-stamped with the VM's `ext_agent_host_id`. The VM owns
   refreshes afterward; the laptop-side copy should be deleted.

## Configure the model in DeerFlow

```yaml
models:
  - name: chatgpt-plan
    display_name: ChatGPT Plan (OAuth)
    use: deerflow.models.chatgpt_plan_provider:ChatGPTPlanChatModel
    model: <slug from the `models` subcommand>
    # account: user@example.com   # REQUIRED once more than one account exists
    # reasoning_effort: medium
    supports_reasoning_effort: true
    supports_thinking: true
```

Notes:

- With exactly one authorized account, `account` may be omitted; with multiple
  it is mandatory — the store refuses to silently pick one (wrong-account
  billing prevention).
- Generic OpenAI kwargs (`temperature`, `top_p`, `max_tokens`, `metadata`, …)
  are dropped with a warning — the route rejects them upstream; this is
  deliberate, not silent.
- `request_admission` policies work as usual (the provider honors
  `rate_limiter`).

## Sign out / revoke

```bash
.venv/bin/python -m deerflow.models.chatgpt_plan_oauth logout --account user@example.com
```

Posts `token_type_hint=refresh_token` to the OIDC `revocation_endpoint` (from
discovery), then clears local tokens while retaining the account↔client_id
mapping (required for a clean re-sign-in).

## Security properties

- Tokens live only under the credentials dir, `0600`, atomic writes; nothing
  is logged, sent to the frontend, attached to artifacts, or handed to agent
  tools. `redacted_dict()` is what `accounts` prints.
- Refresh tokens rotate; refresh is serialized by an on-disk `flock` so racing
  processes can't spend a rotated token.
- The callback listener binds `127.0.0.1` only (never `localhost`, never a
  wildcard) and never logs the callback path (which carries the code).
- Model construction fails fast when no authorized account exists; inference
  re-checks the inference scope on every token refresh.

## Known limitations / blockers

- **Live OAuth + inference are not yet proven** — the Authorization-Code+PKCE
  dance requires the founder's own browser sign-in. Everything else is
  implemented and covered by mocked tests.
- Plus-plan 5h shared usage window; Pro exempt.
- Preview restrictions listed above (no hosted tools, no param knobs).
- Remotely hosted / commercial EWCP is **blocked pending OpenAI approval**
  (interest form) — out of scope for this PoC.
- Host-specific attribution of transferred sessions is a documented gap on
  OpenAI's side.
