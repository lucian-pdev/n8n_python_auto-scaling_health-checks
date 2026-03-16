# N8N Python Executor

A FastAPI-based Python execution service designed for n8n workflow automation. Executes user-provided Python scripts in isolated worker processes with Prometheus metrics and Grafana visualization.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [System Components](#system-components)
3. [Data Flow](#data-flow)
4. [API Reference](#api-reference)
5. [Metrics & Monitoring](#metrics--monitoring)
6. [Deployment Guide](#deployment-guide)
7. [n8n Integration](#n8n-integration)
8. [Security Considerations](#security-considerations)
9. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
┌─────────────┐     HTTP POST      ┌─────────────────┐
│   n8n       │ ─────────────────▶ │  python-api     │
│  (5678)     │  {data, code_file} │   (FastAPI)     │
└─────────────┘                    │                 │
                                   │  ┌─────────────┐│
                                   │  │ Worker Pool ││
                                   │  │ 4 processes ││
                                   │  │ DRAIN/STOP  ││
                                   │  └─────────────┘│
                                   └────────┬────────┘
                                            │
                              ┌─────────────┼─────────────┐
                              ▼             ▼             ▼
                        ┌─────────┐   ┌──────────┐  ┌──────────┐
                        │Prometheus│   │  Grafana │  │ cAdvisor │
                        │ (9090)  │   │  (3000)  │  │  (8080)  │
                        └─────────┘   └──────────┘  └──────────┘
```

---

## System Components

| Component | Purpose | Port | Image/Build |
|-----------|---------|------|-------------|
| **python-api** | Execute Python scripts in worker pool | 8000 | `Dockerfile` |
| **n8n** | Workflow automation platform | 5678 | `n8nio/n8n:latest` |
| **prometheus** | Metrics collection and storage | 9090 | `prom/prometheus:v3.10.0` |
| **grafana** | Metrics visualization dashboards | 3000 | `grafana/grafana:main-ubuntu` |
| **cadvisor** | Container resource metrics | 8080 | `gcr.io/cadvisor/cadvisor:v0.47.2` |
| **node-exporter** | Host system metrics | 9100 | `prom/node-exporter:v1.7.0` |

---

## Data Flow

### Request Lifecycle

| Step | Component | Action |
|------|-----------|--------|
| 1 | n8n | HTTP POST to `python-api:8000/execute` |
| 2 | python-api | Load script from `/app/scripts/{code_file_name}` |
| 3 | python-api | Submit job to worker pool queue |
| 4 | Worker | `exec()` script with `data` in local scope |
| 5 | Worker | Return `result` via result queue |
| 6 | python-api | Respond with `{"result": ...}` |
| 7 | Prometheus | Scrape metrics from `/metrics` endpoint |

### Worker Signals

| Signal | Behavior | Use Case |
|--------|----------|----------|
| `STOP` | Immediate exit, drops current job | Shutdown |
| `DRAIN` | Finish current job, then exit | Graceful restart (every 5 min) |

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
  "scripts": [str, str]
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

### Prerequisites

- Ubuntu 24.04 Server LTS
- Docker Engine with Compose plugin

### Host Setup

```bash
# 1. Transfer project to VM
tar czf n8n-python-project.tar.gz n8n-python-project/
scp n8n-python-project.tar.gz user@VM_IP:/home/user/

# 2. On VM: extract and deploy
tar xzf n8n-python-project.tar.gz
cd n8n-python-project

# 3. Build and start
docker compose build
docker compose up -d

# 4. Fix n8n permissions (first run only)
sudo chown -R 1000:1000 n8n-data/
sudo chown -R 1000:1000 scripts/

# 5. Verify
docker compose ps
docker compose logs -f python-api
```

### Directory Structure

```
n8n-python-project/
├── docker-compose.yml          # Service orchestration
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

---

## Security Considerations

| Layer | Mitigation |
|-------|-----------|
| Script execution | `exec()` in isolated subprocess; scripts vetted before deployment |
| File access | `os.path.basename()` prevents directory traversal |
| Container escape | Non-root user, read-only script mount |
| Network | Internal Docker networks; only `python-api:8000` and `n8n:5678` exposed |

**Note:** `exec()` runs with full Python capabilities. All scripts must be reviewed before deployment to the `scripts/` directory.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `404 Script not found` | File missing or wrong name | Check `./scripts/` on host; verify `code_file_name` |
| `result is None` | Script didn't assign `result` | Ensure script sets `result = ...` |
| Worker timeout | Script runs >60s | Optimize script or increase timeout |
| Port 8000 in use | Another service bound | `sudo lsof -i :8000` to find conflict |
| n8n permission denied | `n8n-data` owned by root | `sudo chown -R 1000:1000 n8n-data/` |
| Grafana no data | Prometheus not scraping | Check `http://VM_IP:9090/targets` all UP |

### Log Locations

| Service | Command |
|---------|---------|
| python-api | `docker logs python-api` |
| n8n | `docker logs n8n` |
| All | `docker compose logs -f` |

---

## Maintenance

### Worker Restart Cycle

Workers automatically restart every 5 minutes via `restart_loop()` to prevent memory leaks. Uses `DRAIN` signal for graceful handoff.

### Script Updates

Scripts are cached by modification time. To force reload:

1. Modify script file on host
2. mtime change detected on next request
3. New content loaded and cached

### Backup

| Path | Contents |
|------|----------|
| `./n8n-data/` | n8n workflows, credentials |
| `./scripts/` | User Python scripts |
| `prometheus_data/` | Metrics history (optional) |

---

## License

MIT License - See individual component licenses for dependencies.