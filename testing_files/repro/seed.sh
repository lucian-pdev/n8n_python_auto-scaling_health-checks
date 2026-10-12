#!/bin/bash
# Rehearsal only: make a fresh install look used. n8n owner + saved login cookie, an encrypted credential,
# a scheduled workflow that writes a heartbeat row every minute with it, an active production webhook.
set -euo pipefail
cd "${REPRO_DIR:-$PWD}"
dc() { sudo docker compose "$@"; }
envv() { sed -n "s/^$1=//p" .env | sed -E "s/[[:space:]]+#.*$//; s/[[:space:]]+$//; s/^['\"]//; s/['\"]$//" | head -1; }
PG_USER="$(envv POSTGRES_USERNAME)"; PG_PW="$(envv POSTGRES_PASSWORD)"
R="${REPRO_STATE:-$HOME/repro-state}"; mkdir -p "$R"
BROWSER_ID=rehearsal-browser-0001

echo "== heartbeat table (own database next to n8n's)"
dc exec -T postgres psql -U "$PG_USER" -d n8n -qc "create database rehearsal"
dc exec -T postgres psql -U "$PG_USER" -d rehearsal -qc "create table heartbeat (id serial primary key, at timestamptz default now(), source text)"

echo "== n8n owner (first-run setup) + saved login cookie"
for _ in $(seq 1 60); do      # n8n answers HTTP before its REST API is ready
    [[ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5678/healthz/readiness)" == 200 ]] && break
    sleep 5
done
for try in 1 2 3 4 5; do
    rm -f "$R"/cookies.txt
    # browser-id: n8n binds the session to the browser's id (2.43+ rejects the cookie without the same id);
    # real browsers always send it, so the checks send this fixed one
    body="$(curl -s -c "$R"/cookies.txt -H 'Content-Type: application/json' -H "browser-id: $BROWSER_ID" \
      -d '{"email":"owner@rehearsal.test","firstName":"Ops","lastName":"Owner","password":"Rehearsal-Pass-1"}' \
      http://127.0.0.1:5678/rest/owner/setup)"
    awk '$6 == "n8n-auth" {print $7}' "$R"/cookies.txt > "$R"/n8n-auth.cookie
    grep -q '"email":"owner@rehearsal.test"' <<< "$body" && [[ -s "$R"/n8n-auth.cookie ]] && break
    echo "owner setup not effective yet (try $try): ${body:0:120}"; sleep 10
done
[[ -s "$R"/n8n-auth.cookie ]] || { echo "owner setup failed: no login cookie"; exit 1; }
code="$(curl -s -o /dev/null -w '%{http_code}' -H "browser-id: $BROWSER_ID" -H "Cookie: n8n-auth=$(cat "$R"/n8n-auth.cookie)" http://127.0.0.1:5678/rest/login)"
echo "session check: HTTP $code"; [[ "$code" == 200 ]] || exit 1

echo "== encrypted credential + 2 active workflows"
cat > "$R"/creds.json <<JSON
[{"id":"rehPgCred0000001","name":"Rehearsal Postgres","type":"postgres",
  "data":{"host":"postgres","database":"rehearsal","user":"$PG_USER","password":"$PG_PW","port":5432,"ssl":"disable"}}]
JSON
cat > "$R"/workflows.json <<'JSON'
[{"id":"rehHeartbeat0001","name":"Heartbeat (uses the encrypted credential)","active":false,"settings":{},
  "nodes":[
    {"id":"a1","name":"Every minute","type":"n8n-nodes-base.scheduleTrigger","typeVersion":1.2,"position":[0,0],
     "parameters":{"rule":{"interval":[{"field":"minutes","minutesInterval":1}]}}},
    {"id":"a2","name":"Insert heartbeat","type":"n8n-nodes-base.postgres","typeVersion":2.5,"position":[220,0],
     "parameters":{"operation":"executeQuery","query":"insert into heartbeat (source) values ('n8n')"},
     "credentials":{"postgres":{"id":"rehPgCred0000001","name":"Rehearsal Postgres"}}}],
  "connections":{"Every minute":{"main":[[{"node":"Insert heartbeat","type":"main","index":0}]]}}},
 {"id":"rehWebhook000001","name":"Production webhook","active":false,"settings":{},
  "nodes":[
    {"id":"b1","name":"Webhook","type":"n8n-nodes-base.webhook","typeVersion":2,"position":[0,0],"webhookId":"5d7c1a2e-0b1f-4c33-9d2a-rehearsal0001",
     "parameters":{"path":"rehearsal-hook","httpMethod":"POST","responseMode":"onReceived"}}],
  "connections":{}}]
JSON
sudo docker cp "$R"/creds.json n8n-main:/tmp/creds.json
sudo docker cp "$R"/workflows.json n8n-main:/tmp/workflows.json
dc exec -T n8n-main n8n import:credentials --input=/tmp/creds.json 2>&1 | tail -1
dc exec -T n8n-main n8n import:workflow --input=/tmp/workflows.json 2>&1 | tail -1
for id in rehHeartbeat0001 rehWebhook000001; do
    dc exec -T n8n-main n8n publish:workflow --id=$id 2>&1 | tail -1 \
      || dc exec -T n8n-main n8n update:workflow --id=$id --active=true 2>&1 | tail -1
done
dc restart n8n-main n8n-worker >/dev/null      # CLI activation is picked up on start
echo "seeded"
