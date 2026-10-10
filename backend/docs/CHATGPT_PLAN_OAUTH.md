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
- On `subscription_sharing_usage_limit_exceeded` (HTTP 429), the documented
  recovery is: **pause** new plan-billed requests and link the user to
  ChatGPT settings → Usage. Do not assume the plan is empty, do not infer a
  reset time from the code alone — an app-specific limit can also apply.
  (No per-tier reset window is documented for this flow; anything that names
  one would be [unverified] and is deliberately not claimed here.)
- Preview surface: no `temperature`/`top_p`/`max_output_tokens`/`metadata`/
  `previous_response_id`, no hosted tools (image gen, file search, Code
  Interpreter, computer use, hosted MCP/connectors). Function/custom tools
  only. `store=false`, `stream=true` are mandatory.

## Files

- `packages/harness/deerflow/models/chatgpt_plan_oauth.py` — OAuth layer + CLI
- `packages/harness/deerflow/models/chatgpt_plan_provider.py` — `ChatGPTPlanChatModel`
- `tests/test_chatgpt_plan_oauth.py`, `tests/test_chatgpt_plan_provider.py` — mocked suite
- `scripts/benchmark/chatgpt_plan_eval/` — DeerFlow-level benchmark harness

## Local authorization runbook (founder runs this on his machine)

The browser dance runs on **your** machine — Devin/CI cannot and must not do
it. Do not paste ChatGPT credentials anywhere. All commands run from
`backend/`; `.venv` must exist (`cd backend && uv sync` if not).

Each step is a live gate. Stop and fix at the first failing gate.

**Gate 1 — consent grants the inference scope** (`chatgpt.tokens.use.direct`)

```bash
.venv/bin/python -m deerflow.models.chatgpt_plan_oauth login
```

Expected output:

```
Open this URL to sign in with ChatGPT:
  https://auth.openai.com/api/accounts/authorize?response_type=code&...

Signed in as <email>; client_id oaiapp_.... Credentials saved under <runtime_home>/chatgpt-plan.
```

The listener binds `http://127.0.0.1:<port>/auth/callback` (default 1455,
ephemeral fallback) and opens the browser. `--no-browser` prints only.
PASS = the "Signed in as" line (NOT the `BUT the grant lacks
'chatgpt.tokens.use.direct'` variant — that means consent was declined for
plan usage; re-run `login --account <email>` and approve the scope).
Credential file: `<runtime_home>/chatgpt-plan/accounts/<client_id>.json`,
`0600`, atomic write, ID token verified against OpenAI JWKS.

**Gate 2 — model discovery on the real account**

```bash
.venv/bin/python -m deerflow.models.chatgpt_plan_oauth accounts   # redacted view (no tokens)
.venv/bin/python -m deerflow.models.chatgpt_plan_oauth models
```

Expected: `accounts` prints a redacted record (email, client_id, scopes,
has_* flags only). `models` prints the account's catalog:

```
gpt-5.2-codex                            GPT-5.2 Codex
...
```

Pick a `slug` for `model:` in config.yaml. Reminder: a successful list is
NOT proof of inference — gate 3 is.

**Gate 3 — one streamed Responses request completes**

```bash
CHATGPT_PLAN_MODEL=<slug> \
.venv/bin/python -m scripts.benchmark.chatgpt_plan_eval smoke --arm chatgpt_plan
```

Expected output (real `POST /v1/responses` through the provider):

```json
{
  "ok": true,
  "content": "ok",
  "latency_ms": <int>,
  "usage_metadata": {"input_tokens": <int>, "output_tokens": <int>, "total_tokens": <int>},
  "billing_source": "chatgpt_plan",
  "provider": "chatgpt_plan_oauth"
}
```

PASS = `ok: true`. `ok: false` with `InferenceScopeMissingError` = gate 1
failed; `ChatGPTPlanUsageLimitError`/`code: subscription_sharing_usage_limit_exceeded`
= plan limit hit (documented recovery: pause + ChatGPT settings → Usage);
`ChatGPTPlanNotEligibleError`/`user_not_eligible` = account not eligible —
do NOT retry OAuth in a loop.

**Gate 4 — one real function-calling turn**

```bash
.venv/bin/python -m scripts.benchmark.chatgpt_plan_eval run \
    --arm chatgpt_plan --model <slug> \
    --tasks scripts/benchmark/chatgpt_plan_eval/tasks.json \
    --output-dir /tmp/chatgpt-plan-eval
```

Expected: `t2-arith-tool` and `t3-two-step-tool` complete with
`tool_call_success == tool_calls` (the tool args parse and execute, the
`function_call_output` round-trips). PASS = `correct_rate` ≈ 1.0 with
`tool_call_success_rate == 1.0`. A `subscription_sharing_unsupported_capability`
failure here means a hosted/tool type leaked into `tools` — bug, report it.

**Gate 5 — usage + attribution recorded**

`results.json` in the output dir: every result carries `inference_calls`,
`input_tokens`, `output_tokens`, `latency_ms`, `failure_cause`; the model's
response metadata carries `billing_source="chatgpt_plan"`,
`provider="chatgpt_plan_oauth"`, `auth_mode="oauth_chatgpt_plan"`. These are
measured tokens — NOT billed cost; any `pricing` block yields an
API-equivalent estimate only.

**Gate 6 — revoked/expired grant fails appropriately**

```bash
.venv/bin/python -m deerflow.models.chatgpt_plan_oauth logout --account <email>
# → "Remote session revoked and local tokens cleared."
CHATGPT_PLAN_MODEL=<slug> \
.venv/bin/python -m scripts.benchmark.chatgpt_plan_eval smoke --arm chatgpt_plan
```

Expected after logout: `{"ok": false, "error": "CredentialNotFoundError: ..."}`
(exit 1) — no silent fallback to another credential. Remote revocation
(ChatGPT settings → disconnect app) surfaces as
`subscription_sharing_invalid_user`/`ChatGPTPlanInvalidUserError` on the next
call or an `invalid_grant`-family refresh error — re-run `login` to recover.
Do NOT erase credentials on transient network failures.

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
- Plan-billed requests can hit `subscription_sharing_usage_limit_exceeded`
  (pause + settings → Usage; no documented per-tier reset window for this
  flow — any specific window named elsewhere is [unverified]).
- Preview restrictions listed above (no hosted tools, no param knobs).
- Remotely hosted / commercial EWCP is **blocked pending OpenAI approval**
  (interest form) — out of scope for this PoC.
- Host-specific attribution of transferred sessions is a documented gap on
  OpenAI's side.
