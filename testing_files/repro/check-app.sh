#!/bin/bash
# Rehearsal: does the app work? Same checks before and after. The scheduled workflow keeps writing with the
# encrypted credential, the webhook answers (direct and through Traefik), the saved login is still valid.
set -uo pipefail
cd "${REPRO_DIR:-$PWD}"
R="${REPRO_STATE:-$HOME/repro-state}"
fails=0; pass() { echo "  PASS  $*"; }; fail() { echo "  FAIL  $*"; fails=$((fails + 1)); }
envv() { sed -n "s/^$1=//p" .env | sed -E "s/[[:space:]]+#.*$//; s/[[:space:]]+$//; s/^['\"]//; s/['\"]$//" | head -1; }
beats() { sudo docker compose exec -T postgres psql -U "$(envv POSTGRES_USERNAME)" -d rehearsal -Atc 'select count(*) from heartbeat' 2>/dev/null; }

for _ in $(seq 1 60); do [[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5678/healthz/readiness)" == 200 ]] && break; sleep 5; done
before="$(beats)"
for _ in $(seq 1 30); do sleep 5; [[ "$(beats)" -gt "${before:-0}" ]] && break; done
after="$(beats)"
[[ "${after:-0}" -gt "${before:-0}" ]] && pass "scheduled workflow wrote with the encrypted credential ($before -> $after rows)" \
    || fail "no new heartbeat rows in 150 s (credential, worker or scheduler broken)"
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:5678/webhook/rehearsal-hook)"
[[ "$code" == 200 ]] && pass "production webhook answers" || fail "webhook -> $code"
domain="$(envv DOMAIN)"
code="$(curl -sk -o /dev/null -w '%{http_code}' -X POST --resolve "$domain:443:127.0.0.1" "https://$domain/webhook/rehearsal-hook")"
[[ "$code" == 200 ]] && pass "webhook through Traefik (https://$domain/webhook/...)" || fail "webhook via Traefik -> $code"
BID=rehearsal-browser-0001
ver="$(sudo docker compose exec -T n8n-main n8n --version 2>/dev/null | tr -d '\r')"
[[ -s "$R/n8n-version-first.txt" ]] || echo "$ver" > "$R/n8n-version-first.txt"     # first run = baseline
first="$(cat "$R/n8n-version-first.txt")"
[[ -s "$R/n8n-auth.cookie" ]] && code="$(curl -s -o /dev/null -w '%{http_code}' -H "browser-id: $BID" -H "Cookie: n8n-auth=$(cat "$R/n8n-auth.cookie")" http://127.0.0.1:5678/rest/login)" || code="no saved cookie"
if [[ "$code" == 200 ]]; then
    pass "saved login still valid (users stay logged in)"
elif [[ "$ver" != "$first" ]]; then
    # n8n itself may invalidate sessions across versions (seen 2.41 -> 2.43 with identical JWT secret/encryption key):
    # then the keys are fine if a fresh login works, and users only have to log in once more
    fresh="$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -H "browser-id: $BID" \
        -d '{"emailOrLdapLoginId":"owner@rehearsal.test","password":"Rehearsal-Pass-1"}' http://127.0.0.1:5678/rest/login)"
    if [[ "$fresh" == 200 ]]; then
        echo "  WARN  saved login -> $code after n8n $first -> $ver: n8n dropped existing sessions; a fresh login works (same keys). Tell users they must log in again."
    else
        fail "saved login -> $code and a fresh login -> $fresh after n8n $first -> $ver (auth broken)"
    fi
else
    fail "saved login -> $code with the same n8n $ver (users would be logged out: JWT secret / encryption key changed?)"
fi
echo "  INFO  n8n $ver"
exit $(( fails > 0 ))
