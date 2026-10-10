#!/usr/bin/env bash
# edge_probe.sh — EWCP edge SSE/Auth probe set against $EDGE_HOST.
#
# PREPARED OFFLINE for the A7A readiness sprint: run it once DNS delegation
# lands (founder-owned — do NOT touch DNS/NS/MX/TXT/hostnames/Access config).
#
# What it checks (mirrors docs/vnext/A7A_SSE_RECONNECT.md probe set + auth):
#   1. Unauthenticated /            -> 302 to Cloudflare Access (auth gate works)
#   2. Unauthenticated /api/...     -> 302/401 — API is behind Access too
#   3. Unauthenticated /verify      -> MUST go through Access — NO bypass
#   4. Unauthenticated /_next/...   -> MUST go through Access — NO bypass
#   5. With CF Access service token -> app reachable, SSE join works end-to-end
#
# Usage:
#   EDGE_HOST=https://edge.example.com ./scripts/edge_probe.sh           # unauth'd checks only
#   EDGE_HOST=... CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=... \
#     EDGE_COOKIE_JAR=/path/to/manual-otp.jar ./scripts/edge_probe.sh    # authed probes
#
# Access OTP is interactive — this script cannot complete it. Two ways through:
#   a) Service token: CF-Access-Client-Id/CF-Access-Client-Secret headers
#      (non-interactive path; create a scoped token in Zero Trust, keep it
#      in env — never commit it).
#   b) Manual OTP once in a browser, then export the cookie jar
#      (Netscape format) to $EDGE_COOKIE_JAR.
# Neither path weakens Access: the script asserts the 302 gate exists first
# and never rewrites Host or spoofs headers.

set -u
HOST="${EDGE_HOST:?set EDGE_HOST=https://edge.example.com}"
HOST="${HOST%/}"
JAR="${EDGE_COOKIE_JAR:-/tmp/edge_probe.jar}"
PASS=0; FAIL=0; SKIP=0

ok()   { PASS=$((PASS+1)); echo "PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL  $1"; }
skip() { SKIP=$((SKIP+1)); echo "SKIP  $1 — $2"; }

auth_headers=()
curl_base=(curl -sS -o /dev/null -w "%{http_code}" --max-time 15)
if [ -n "${CF_ACCESS_CLIENT_ID:-}" ] && [ -n "${CF_ACCESS_CLIENT_SECRET:-}" ]; then
  auth_headers=(-H "CF-Access-Client-Id: ${CF_ACCESS_CLIENT_ID}" -H "CF-Access-Client-Secret: ${CF_ACCESS_CLIENT_SECRET}")
  echo "== service-token mode (env CF_ACCESS_CLIENT_ID present)"
elif [ -f "$JAR" ]; then
  echo "== cookie-jar mode ($JAR)"
else
  echo "== unauthenticated mode (no service token, no jar — auth'd probes will SKIP)"
fi

code() { "${curl_base[@]}" "$@" 2>/dev/null || echo "000"; }

# --- 1-4: Access gate on every public surface ------------------------------
for path in "/" "/api/ewcp/runs" "/verify" "/_next/static/probe.css"; do
  c=$(code "$HOST$path")
  case "$c" in
    302|301) loc=$(curl -sS -o /dev/null -w "%{redirect_url}" --max-time 15 "$HOST$path")
             case "$loc" in *cloudflareaccess.com*|*"/cdn-cgi/access/"*) ok "$path -> $c to Access" ;;
                             *) bad "$path -> $c but NOT to Access ($loc)" ;; esac ;;
    401|403) ok "$path -> $c (denied at edge)" ;;
    000)     bad "$path unreachable (DNS/TLS not up?)" ;;
    *)       bad "$path -> $c (expected 302-to-Access or 401/403 — public bypass?)" ;;
  esac
done

# --- 5: authed probes -------------------------------------------------------
authed=0
if [ ${#auth_headers[@]} -gt 0 ] || [ -f "$JAR" ]; then authed=1; fi

if [ "$authed" -eq 0 ]; then
  skip "authenticated surface" "no service token / jar — rerun with CF_ACCESS_* or EDGE_COOKIE_JAR"
else
  jar_arg=(); [ -f "$JAR" ] && jar_arg=(-b "$JAR" -c "$JAR")
  c=$(code "${jar_arg[@]}" "${auth_headers[@]}" "$HOST/")
  [ "$c" = "200" ] && ok "authed / -> 200" || bad "authed / -> $c"

  c=$(code "${jar_arg[@]}" "${auth_headers[@]}" "$HOST/api/v1/config")
  case "$c" in 200|401) ok "authed /api/v1/config -> $c (edge passes; app-level auth answers)" ;;
               *) bad "authed /api/v1/config -> $c" ;; esac

  # --- SSE probes: reuse the A7A #66 set -----------------------------------
  # a) login + CSRF (app-level, same as local bench)
  c=$(code "${jar_arg[@]}" "${auth_headers[@]}" -c "$JAR" -X POST \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "username=${EDGE_USER:?set EDGE_USER for authed probes}&password=${EDGE_PASS:?set EDGE_PASS}" \
        "$HOST/api/v1/auth/login/local")
  if [ "$c" = "200" ] || [ "$c" = "204" ]; then
    ok "app login/local -> $c"
    CSRF=$(awk '$6=="csrf_token"{print $NF}' "$JAR" | tail -1)
    # b) launch a run, c) join stream, d) post-end join
    ER=$(curl -sS "${jar_arg[@]}" "${auth_headers[@]}" -X POST \
          -H "X-CSRF-Token: ${CSRF:-missing}" \
          -F "intent=edge probe ping" -F "tenant_id=demo" \
          "$HOST/api/ewcp/runs" | python3 -c "import json,sys;print(json.load(sys.stdin)['run']['execution_run_id'])" 2>/dev/null || true)
    if [ -z "${ER:-}" ]; then bad "launch run (POST /api/ewcp/runs) failed"; else
      ok "launched $ER"
      TR=$(curl -sS "${jar_arg[@]}" "${auth_headers[@]}" "$HOST/api/ewcp/runs/$ER" \
           | python3 -c "import json,sys;r=json.load(sys.stdin)['run'];print(r['thread_id'],r['run_id'])")
      set -- $TR
      body=$(curl -sS -N --max-time 20 "${jar_arg[@]}" "${auth_headers[@]}" \
             -H "Accept: text/event-stream" "$HOST/api/threads/$1/runs/$2/join" | head -40)
      echo "$body" | grep -q "^event:" && ok "SSE join streamed events (edge does not buffer SSE)" \
                                          || bad "SSE join produced no events — proxy buffering?"
      sleep 2
      end=$(curl -sS -N --max-time 20 "${jar_arg[@]}" "${auth_headers[@]}" \
            "$HOST/api/threads/$1/runs/$2/join" | head -3)
      echo "$end" | grep -q "event: end" && ok "post-end join -> end (retained buffer cleaned at edge too)" \
                                          || bad "post-end join unexpected: $(echo "$end" | head -1)"
      # stale-cursor contract (well-formed {ts}-{seq} below watermark)
      gap=$(curl -sS -N --max-time 15 "${jar_arg[@]}" "${auth_headers[@]}" \
            -H "Last-Event-ID: 1-1" "$HOST/api/threads/$1/runs/$2/join" | head -3)
      echo "$gap" | grep -Eq "stream_replay_gap|event: end" \
          && ok "stale cursor -> gap|end contract intact through edge" \
          || bad "stale cursor unexpected: $(echo "$gap" | head -1)"
    fi
  else
    skip "app-level SSE probes" "login/local -> $c (check EDGE_USER/EDGE_PASS)"
  fi
fi

echo "----"
echo "edge_probe: $PASS pass / $FAIL fail / $SKIP skip"
[ "$FAIL" -eq 0 ]
