#!/bin/bash

# Scale n8n-worker based on the n8n job queue in Redis (Bull queue "jobs")

# place at /usr/local/bin/autoscaler.sh

# /usr/local/bin/autoscaler.sh

# Settings come from the project's .env via autoscaler.service (EnvironmentFile=)
PROJECT_DIR="${PROJECT_DIR:?PROJECT_DIR not set - run via autoscaler.service}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
SERVICE_NAME="n8n-worker"
MIN_REPLICAS="${AUTOSCALE_MIN_REPLICAS:-1}"
MAX_REPLICAS="${AUTOSCALE_MAX_REPLICAS:-10}"
QUEUE_THRESHOLD_UP="${AUTOSCALE_THRESHOLD_UP:-4}"
WORKER_CONCURRENCY=4   # must match `worker --concurrency=N` in docker-compose.yml
SCALE_COOLDOWN="${AUTOSCALE_COOLDOWN:-60}"

LOCK_FILE="/tmp/autoscaler.lock"
LAST_SCALE_FILE="/tmp/autoscaler_last_scale"

# Prevent concurrent runs
if [ -f "$LOCK_FILE" ]; then
    pid=$(cat "$LOCK_FILE" 2>/dev/null)
    if kill -0 "$pid" 2>/dev/null; then
        echo "Autoscaler already running (PID: $pid)"
        exit 0
    fi
    # Stale lock
    rm -f "$LOCK_FILE"
fi

echo $$ > "$LOCK_FILE"

cleanup() {
    rm -f "$LOCK_FILE"
}
trap cleanup EXIT

compose() {
    docker compose -f "$PROJECT_DIR/docker-compose.yml" --project-directory "$PROJECT_DIR" "$@"
}

# Get current replicas via docker compose
get_replicas() {
    compose ps -q "$SERVICE_NAME" 2>/dev/null | wc -l
}

# # Get CPU from python-api container
# get_cpu() {
#     curl -sf "$PROMETHEUS_URL/api/v1/query?query=avg(rate(container_cpu_usage_seconds_total%7Bname%3D%22python-api%22%7D%5B5m%5D))*100" 2>/dev/null | \
#         jq -r '.data.result[0].value[1] // empty'
# }

redis_cli() {
    compose exec -T redis redis-cli ${REDIS_PASSWORD:+-a "$REDIS_PASSWORD" --no-auth-warning} "$@"
}

# Jobs waiting for a free worker slot / jobs running now
get_waiting() {
    redis_cli LLEN bull:jobs:wait 2>/dev/null | tr -dc '0-9'
}
get_active() {
    redis_cli LLEN bull:jobs:active 2>/dev/null | tr -dc '0-9'
}

current=$(get_replicas)
waiting=$(get_waiting)
active=$(get_active)

# Redis unreachable: don't guess, leave replicas as they are
if [ -z "$waiting" ] || [ -z "$active" ]; then
    echo "$(date): Could not read queue from Redis, skipping"
    exit 0
fi

echo "$(date): waiting=$waiting active=$active replicas=$current"

# Read last scale time
last_scale=0
[ -f "$LAST_SCALE_FILE" ] && last_scale=$(cat "$LAST_SCALE_FILE")
now=$(date +%s)

# Cooldown check
if [ $((now - last_scale)) -lt $SCALE_COOLDOWN ]; then
    exit 0
fi

new=$current

# Scale up: backlog is building
if [ "$waiting" -gt "$QUEUE_THRESHOLD_UP" ] && [ "$current" -lt "$MAX_REPLICAS" ]; then
    new=$((current + 1))
fi

# Scale down: nothing waiting and one fewer worker can still hold all running jobs
if [ "$waiting" -eq 0 ] && [ "$active" -le $(( (current - 1) * WORKER_CONCURRENCY )) ] && [ "$current" -gt "$MIN_REPLICAS" ]; then
    new=$((current - 1))
fi

# Clamp
[ "$new" -lt "$MIN_REPLICAS" ] && new=$MIN_REPLICAS
[ "$new" -gt "$MAX_REPLICAS" ] && new=$MAX_REPLICAS

# Apply
if [ "$new" -ne "$current" ]; then
    echo "$(date): Scaling $SERVICE_NAME: $current -> $new (waiting: $waiting, active: $active)"
    compose up -d --scale "$SERVICE_NAME=$new" --no-recreate
    echo "$now" > "$LAST_SCALE_FILE"
fi
