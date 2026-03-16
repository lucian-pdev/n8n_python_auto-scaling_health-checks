#!/bin/bash

# Scale n8n-worker based on Redis queue depth or python-api load

# place at /usr/local/bin/autoscaler.sh

# /usr/local/bin/autoscaler.sh

PROMETHEUS_URL="http://localhost:9090" # or "http://prometheus:9090"
SERVICE_NAME="n8n-worker"
MIN_REPLICAS=1
MAX_REPLICAS=10
QUEUE_THRESHOLD_UP=20
QUEUE_THRESHOLD_DOWN=5
SCALE_COOLDOWN=60

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

# Get current replicas via docker compose
get_replicas() {
    cd /home/user/n8n-python-project 2>/dev/null || cd /root/n8n-python-project 2>/dev/null || cd /app 2>/dev/null
    docker compose ps -q "$SERVICE_NAME" 2>/dev/null | wc -l
}

# # Get CPU from python-api container
# get_cpu() {
#     curl -sf "$PROMETHEUS_URL/api/v1/query?query=avg(rate(container_cpu_usage_seconds_total%7Bname%3D%22python-api%22%7D%5B5m%5D))*100" 2>/dev/null | \
#         jq -r '.data.result[0].value[1] // empty'
# }

# Get n8n queue depth from Redis or approximate from python-api load
get_queue_depth() {
    # Option 1: If n8n exposes queue metrics to Prometheus
    # curl -sf "$PROMETHEUS_URL/api/v1/query?query=n8n_queue_depth" 2>/dev/null | jq -r '.data.result[0].value[1] // 0'
    
    # Option 2: Use python-api request rate as proxy for load
    curl -sf "$PROMETHEUS_URL/api/v1/query?query=rate(py_api_requests_total[5m])" 2>/dev/null | \
        jq -r '[.data.result[].value[1] | tonumber] | add // 0'
}

current=$(get_replicas)
queue=$(get_queue_depth)

[ -z "$queue" ] && queue=0

# Read last scale time
last_scale=0
[ -f "$LAST_SCALE_FILE" ] && last_scale=$(cat "$LAST_SCALE_FILE")
now=$(date +%s)

# Cooldown check
if [ $((now - last_scale)) -lt $SCALE_COOLDOWN ]; then
    exit 0
fi

new=$current

# Scale up
if (( $(echo "$queue > $QUEUE_THRESHOLD_UP" | bc -l) )) && [ "$current" -lt "$MAX_REPLICAS" ]; then
    new=$((current + 1))
fi

# Scale down
if (( $(echo "$queue < $QUEUE_THRESHOLD_DOWN" | bc -l) )) && [ "$current" -gt "$MIN_REPLICAS" ]; then
    new=$((current - 1))
fi

# Clamp
[ "$new" -lt "$MIN_REPLICAS" ] && new=$MIN_REPLICAS
[ "$new" -gt "$MAX_REPLICAS" ] && new=$MAX_REPLICAS

# Apply
if [ "$new" -ne "$current" ]; then
    echo "$(date): Scaling $SERVICE_NAME: $current -> $new (queue: $queue)"
    cd "$HOME"/n8n-python-project 2>/dev/null || cd /root/n8n-python-project 2>/dev/null
    docker compose up -d --scale "$SERVICE_NAME=$new" --no-recreate
    echo "$now" > "$LAST_SCALE_FILE"
fi