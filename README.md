# N8N Python Executor

A production-ready FastAPI-based Python execution service designed for n8n workflow automation. Features horizontal scaling with queue-based n8n workers, automatic SSL provisioning, nginx reverse proxy, systemd-based autoscaling, and comprehensive monitoring with Prometheus and Grafana.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [System Components](#system-components)
3. [Data Flow](#data-flow)
4. [API Reference](#api-reference)
5. [Metrics & Monitoring](#metrics--monitoring)
6. [Deployment Guide](#deployment-guide)
7. [Autoscaling & Health Monitoring](#autoscaling--health-monitoring)
8. [n8n Integration](#n8n-integration)
9. [Security Considerations](#security-considerations)
10. [Troubleshooting](#troubleshooting)
11. [Notice](#notice)

---

## Architecture Overview

```
                                    ┌─────────────────┐
                                    │   Internet      │
                                    └────────┬────────┘
                                             │
                                    ┌────────▼────────┐
                                    │  Nginx (443)    │
                                    │  SSL/TLS        │
                                    │  Reverse Proxy  │
                                    └────────┬────────┘
                                             │
                    ┌────────────────────────┼────────────────────────┐
                    │                        │                        │
           ┌────────▼────────┐      ┌───────▼────────┐      ┌────────▼────────┐
           │  /grafana/      │      │      /         │      │   /healthz      │
           │  Grafana (3000) │      │  n8n-main      │      │  Health Check   │
           │  Dashboards     │      │  (5678)        │      │                 │
           └─────────────────┘      └───────┬────────┘      └─────────────────┘
                                             │
                              ┌──────────────┼──────────────┐
                              │              │              │
                    ┌─────────▼─────┐ ┌────▼──────┐ ┌─────▼──────┐
                    │  python-api   │ │   Redis   │ │  n8n-worker│
                    │   (8000)      │ │  (6379)   │ │  (scaled)  │
                    │  Worker Pool  │ │  Queue    │ │            │
                    │  4 processes  │ │           │ │            │
                    └───────┬───────┘ └───────────┘ └────────────┘
                            │
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
        ┌─────────┐   ┌──────────┐  ┌──────────┐
        │Prometheus│   │  cAdvisor │  │node-exporter
        │ (9090)  │   │  (8080)  │  │  (9100)  │
        └─────────┘   └──────────┘  └──────────┘
```

---

## System Components

### Core Application Services

| Component | Purpose | Port | Image/Build |
|-----------|---------|------|-------------|
| **python-api** | Execute Python scripts in worker pool | 8000 | `Dockerfile` |
| **n8n-main** | Workflow automation platform (queue mode) | 5678 | `n8nio/n8n:latest` |
| **n8n-worker** | Scalable queue workers for n8n executions | — | `n8nio/n8n:latest` |
| **redis** | Queue broker for n8n and potential API queue | 6379 | `redis:latest` |

### Monitoring & Observability

| Component | Purpose | Port | Image |
|-----------|---------|------|-------|
| **prometheus** | Metrics collection and storage | 9090 | `prom/prometheus:v3.10.0` |
| **grafana** | Metrics visualization dashboards | 3000 | `grafana/grafana:main-ubuntu` |
| **cadvisor** | Container resource metrics | 8080 | `gcr.io/cadvisor/cadvisor:v0.47.2` |
| **node-exporter** | Host system metrics | 9100 | `prom/node-exporter:v1.7.0` |

### Infrastructure & Automation

| Component | Purpose | Location |
|-----------|---------|----------|
| **nginx** | SSL termination, reverse proxy, static health endpoint | Host systemd |
| **certbot** | Automatic SSL certificate provisioning | Host |
| **autoscaler** | Dynamic n8n-worker scaling based on queue depth | `/usr/local/bin/autoscaler.sh` |
| **nginx-health** | Automatic nginx recovery monitoring | `/usr/local/bin/nginx-health.sh` |

---

## Data Flow

### Request Lifecycle

| Step | Component | Action |
|------|-----------|--------|
| 1 | Client | HTTPS request to `https://n8n.dashboard.com` |
| 2 | nginx | SSL termination, route to appropriate backend |
| 3 | n8n-main | Execute workflow, queue heavy jobs to Redis |
| 4 | n8n-worker | Pull job from Redis queue, execute |
| 5 | n8n (HTTP node) | POST to `http://python-api:8000/execute` |
| 6 | python-api | Load script from `/app/scripts/{code_file_name}` |
| 7 | python-api | Submit job to worker pool queue |
| 8 | Worker | `exec()` script with `data` in local scope |
| 9 | Worker | Return `result` via result queue |
| 10 | python-api | Respond with `{"result": ...}` |
| 11 | Prometheus | Scrape metrics from `/metrics` endpoint |

### Worker Signals (python-api)

| Signal | Behavior | Use Case |
|--------|----------|----------|
| `STOP` | Immediate exit, drops current job | Shutdown |
| `DRAIN` | Finish current job, then exit | Graceful restart (every 5 min) |

### n8n Queue Mode

| Mode | Behavior | Use Case |
|------|----------|----------|
| `queue` | Main instance queues jobs, workers execute | Production, scalable |
| `regular` | Main instance executes everything | Development only |

---

## API Reference

### `POST /execute`

Execute a Python script with provided data.

**Request Body:**
```json
{
  "data": { "any": "json", "structure": true },
  "code_file_name": "script_name.py"
}
```

**Response:**
```json
{
  "result": {
    "job_id": "uuid",
    "result": "<script output>",
    "process_time": 0.123,
    "status": "success|error"
  }
}
```

### `GET /health`

Health check endpoint.

**Response:**
```json
{
  "status": "ok",
  "workers": 4
}
```

### `GET /scripts`

List available Python scripts.

**Response:**
```json
{
  "scripts": ["normalize_mobile.py", "validate_email.py", "geocode_address.py"]
}
```

### `GET /metrics`

Prometheus metrics endpoint (text format).

---

## Metrics & Monitoring

### Custom Application Metrics

Exposed via `prometheus-client` in `main.py`:

| Metric Name | Type | Labels | Description |
|-------------|------|--------|-------------|
| `py_api_requests_total` | Counter | `method`, `endpoint`, `status` | Total HTTP requests |
| `py_api_request_duration_seconds` | Histogram | — | Request latency distribution |
| `py_api_active_workers` | Gauge | — | Current worker process count |
| `py_api_queue_size` | Gauge | — | Job queue size (placeholder: 0) |
| `py_api_worker_restarts_total` | Counter | — | Total worker restarts |
| `py_api_script_executions_total` | Counter | `script_name`, `status` | Script executions by file |

### Infrastructure Metrics

| Source | Metric Prefix | Examples |
|--------|---------------|----------|
| cAdvisor | `container_` | `container_cpu_usage_seconds_total`, `container_memory_usage_bytes` |
| node-exporter | `node_` | `node_cpu_seconds_total`, `node_memory_MemTotal_bytes` |

### Grafana Dashboard Queries

| Panel | Prometheus Query |
|-------|----------------|
| Request Rate | `rate(py_api_requests_total[5m])` |
| Avg Request Duration | `rate(py_api_request_duration_seconds_sum[5m]) / rate(py_api_request_duration_seconds_count[5m])` |
| Active Workers | `py_api_active_workers` |
| Script Executions | `sum by (script_name) (py_api_script_executions_total)` |
| Container CPU % | `rate(container_cpu_usage_seconds_total{name="python-api"}[5m]) * 100` |
| Container Memory | `container_memory_usage_bytes{name="python-api"}` |

---

## Deployment Guide

WARNING:The N8N_ENCRYPTION_KEY hardcoded, Grafana: admin/admin and n8n: admin/pass
at docker-compose.yml (lines 85 and 102) are PLACEHOLDERS.

MODIFY THEM FOR YOUR ENVIRONMENT.

I do NOT take responsability for any use of these files.
I do NOT restrict or monetize them.

### Prerequisites

- Ubuntu 24.04 Server LTS
- Domain name pointing to server (e.g., `n8n.dashboard.com`)
- Root or sudo access

### Quick Deploy (Automatic)

```bash
# 1. Transfer project to VM
tar czf n8n.tar.gz n8n-python-project/
scp n8n.tar.gz user@VM_IP:/home/user/

# 2. On VM: run auto-deploy
chmod +x auto-deploy.sh
sudo ./auto-deploy.sh
```

The `auto-deploy.sh` script automatically:
- Installs Docker, nginx, certbot
- Builds and starts all services
- Configures SSL certificates
- Sets up firewall rules
- Installs systemd autoscaling services
- Fixes permissions

### Manual Deploy

```bash
# 1. Transfer and extract
tar xzf n8n.tar.gz
cd n8n-python-project

# 2. Build and start
docker compose build
docker compose up -d

# 3. Fix permissions (first run only)
sudo chown -R 1000:1000 n8n-data/
sudo chown -R 1000:1000 scripts/

# 4. Configure nginx manually
sudo cp HTTPS/nginx.conf /etc/nginx/sites-available/n8n_grafana.conf
sudo ln -sf /etc/nginx/sites-available/n8n_grafana.conf /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx

# 5. Obtain SSL certificate
sudo certbot --nginx -d n8n.dashboard.com --non-interactive --agree-tos -m your-email@example.com

# 6. Configure firewall
sudo ufw default deny incoming
sudo ufw allow 22/tcp      # SSH
sudo ufw allow 80/tcp      # HTTP redirect
sudo ufw allow 443/tcp     # HTTPS
sudo ufw enable

# 7. Install systemd services
sudo ./autoscaler/setup-systemd.sh

# 8. Verify
docker compose ps
sudo systemctl list-timers --all | grep -E "(autoscaler|nginx-health)"
```

### Directory Structure

```
n8n-python-project/
├── auto-deploy.sh              # One-command deployment script
├── docker-compose.yml          # Service orchestration
├── HTTPS/
│   ├── nginx.conf              # Nginx reverse proxy config
│   ├── firewall_rules.sh       # UFW firewall setup
│   └── nginx_health.sh         # Nginx health check script
├── autoscaler/
│   ├── autoscaler.sh           # n8n-worker scaling logic
│   ├── setup-systemd.sh        # Systemd service installer
├── python-api/
│   ├── Dockerfile              # Python 3.12 + dependencies
│   ├── main.py                 # FastAPI application
│   └── requirements.txt        # Python dependencies
├── prometheus/
│   └── prometheus.yml          # Scrape configuration
├── scripts/                    # User Python scripts (mounted RO)
│   └── *.py
└── n8n-data/                   # n8n persistence (auto-created)
```

### Script Requirements

Place `.py` files in `./scripts/` on host. Scripts must:

- Accept `data` via `local_vars` (injected by `exec()`)
- Assign output to `result` variable
- Handle errors internally or let them propagate

**Example script:**
```python
# scripts/process_person.py
person = data.get("body", {}).get("payload", {}).get("person", {})
result = {
    "email": person.get("email"),
    "name": f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
}
```

---

## Autoscaling & Health Monitoring

### n8n-Worker Autoscaler

Automatically scales `n8n-worker` containers based on Prometheus metrics.

| Setting | Default | Description |
|---------|---------|-------------|
| `MIN_REPLICAS` | 1 | Minimum worker containers |
| `MAX_REPLICAS` | 10 | Maximum worker containers |
| `QUEUE_THRESHOLD_UP` | 20 | Scale up when queue depth exceeds |
| `QUEUE_THRESHOLD_DOWN` | 5 | Scale down when queue below |
| `SCALE_COOLDOWN` | 60s | Minimum time between scaling events |

**How it works:**
1. Queries Prometheus for `rate(py_api_requests_total[5m])` as load proxy
2. Compares against thresholds
3. Scales via `docker compose up -d --scale n8n-worker=N`

**Management:**
```bash
# View timer status
sudo systemctl status autoscaler.timer

# View logs
sudo journalctl -u autoscaler.service -f

# Disable autoscaling
sudo systemctl stop autoscaler.timer
sudo systemctl disable autoscaler.timer
```

### Nginx Health Monitor

Ensures nginx stays running and responsive.

**Checks every 2 minutes:**
1. Is nginx service active?
2. Does `http://localhost/healthz` respond?

**Recovery actions:**
- Service down → `systemctl restart nginx`
- Unresponsive → `systemctl reload nginx`

**Management:**
```bash
# View logs
sudo tail -f /var/log/nginx-health.log
sudo journalctl -u nginx-health.service -f

# Manual test
curl -f http://localhost/healthz
```

### Systemd Services Reference

| Service | Type | Trigger | Purpose |
|---------|------|---------|---------|
| `autoscaler.service` | oneshot | `autoscaler.timer` (30s) | Scale workers |
| `nginx-health.service` | oneshot | `nginx-health.timer` (2m) | Recover nginx |

---

## n8n Integration

### HTTP Request Node Configuration

| Field | Value |
|-------|-------|
| Method | POST |
| URL | `http://python-api:8000/execute` |
| Authentication | None (or configure as needed) |
| Body | JSON |

**Body Content:**
```json
{
  "data": {{ $json }},
  "code_file_name": "your_script.py"
}
```

### Workflow Example

```
[Manual Trigger] → [Set: Define test data] → [HTTP Request: python-api] → [Process result]
```

### Available Scripts Endpoint

Use `GET http://python-api:8000/scripts` to populate dropdowns in n8n.

### Webhook URL Configuration

Set in n8n environment or UI:
```
https://n8n.dashboard.com
```

---

## Security Considerations

| Layer | Mitigation |
|-------|-----------|
| **Transport** | TLS 1.2+ via Let's Encrypt certificates |
| **Script execution** | `exec()` in isolated subprocess; scripts vetted before deployment |
| **File access** | `os.path.basename()` prevents directory traversal |
| **Container escape** | Non-root user, read-only script mount |
| **Network** | Internal Docker networks; only nginx (443) exposed externally |
| **Firewall** | UFW denies incoming except 22, 80, 443 |
| **Secrets** | n8n encryption key in environment; basic auth enabled |

**Note:** `exec()` runs with full Python capabilities. All scripts must be reviewed before deployment to the `scripts/` directory.

---

## Troubleshooting

### Deployment Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Failed to extract n8n.tar.gz` | Archive missing or corrupted | Re-create with `tar czf n8n.tar.gz n8n-python-project/` |
| `Certbot failed` | Domain not pointing to server | Verify DNS A record; check `dig +short n8n.dashboard.com` |
| `docker: command not found` | Docker not installed | Script should install; if failed: `sudo apt install docker-ce` |

### Service Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `404 Script not found` | File missing or wrong name | Check `./scripts/` on host; verify `code_file_name` |
| `result is None` | Script didn't assign `result` | Ensure script sets `result = ...` |
| Worker timeout | Script runs >60s | Optimize script or increase timeout in `main.py` |
| n8n permission denied | `n8n-data` owned by root | `sudo chown -R 1000:1000 n8n-data/` |
| Grafana no data | Prometheus not scraping | Check `http://VM_IP:9090/targets` all UP |

### Scaling Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Workers not scaling up | Queue below threshold or cooldown active | Check `sudo journalctl -u autoscaler.service` |
| Too many workers | Threshold too low or cooldown ignored | Adjust `QUEUE_THRESHOLD_UP` in `/usr/local/bin/autoscaler.sh` |
| Redis connection errors | Redis container down | `docker compose restart redis` |

### Nginx Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `502 Bad Gateway` | Backend container down | `docker compose ps` to check status |
| SSL certificate expired | Certbot renewal failed | `sudo certbot renew --force-renewal` |
| `/healthz` 404 | nginx config not loaded | `sudo nginx -t && sudo systemctl reload nginx` |

### Log Locations

| Service | Command |
|---------|---------|
| python-api | `docker logs python-api` |
| n8n-main | `docker logs n8n-main` |
| n8n-worker | `docker logs n8n-worker` |
| All containers | `docker compose logs -f` |
| Autoscaler | `sudo journalctl -u autoscaler.service -f` |
| Nginx health | `sudo tail -f /var/log/nginx-health.log` |
| Nginx | `sudo journalctl -u nginx -f` |

---

## Maintenance

### Worker Restart Cycle

Workers automatically restart every 5 minutes via `restart_loop()` to prevent memory leaks. Uses `DRAIN` signal for graceful handoff.

### Script Updates

Scripts are cached by modification time. To force reload:

1. Modify script file on host
2. mtime change detected on next request
3. New content loaded and cached

### SSL Certificate Renewal

Certbot auto-renews via systemd timer. Verify with:
```bash
sudo certbot renew --dry-run
```

## NOTICE

This project uses the following third-party software. See their respective repositories for full license texts.

### Python Dependencies

| Package | License |
|---------|---------|
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| Pydantic | MIT |
| prometheus-client | Apache-2.0 |

### Container Images

| Component | License |
|-----------|---------|
| Prometheus | Apache-2.0 |
| Grafana | AGPL-3.0 |
| cAdvisor | Apache-2.0 |
| Node Exporter | Apache-2.0 |
| n8n | [Sustainable Use License](https://github.com/n8n-io/n8n/blob/master/LICENSE.md) |
| Redis | BSD-3-Clause |

**Notes:**
- Grafana is used unmodified via official Docker image. AGPL-3.0 applies if modified or redistributed.
- n8n's Sustainable Use License restricts high-volume production use; review terms before scaling.
- Full license texts: https://opensource.org/licenses

