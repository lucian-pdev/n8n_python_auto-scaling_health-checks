## Metrics & Monitoring

### Architecture

```
┌─────────────────┐     scrape      ┌─────────────────┐     query      ┌─────────────────┐
│  python-api     │◄───────────────►│   Prometheus    │◄──────────────►│     Grafana     │
│  :8000/metrics  │   15s interval  │    :9090        │   (dashboards) │     :3000       │
└─────────────────┘                 └─────────────────┘                └─────────────────┘
         ▲                                   ▲                               
         │                                   │                               
         │         ┌─────────────────┐       │         ┌─────────────────┐    
         │         │    cadvisor     │       │         │  node-exporter  │    
         │         │    :8080        │────────┘         │    :9100        │    
         │         └─────────────────┘                  └─────────────────┘    
         │        (container metrics)                    (host metrics)        
         │                                                                    
         │         ┌─────────────────┐                                         
         └────────►│   Custom app    │                                         
                   │   metrics       │                                         
                   └─────────────────┘                                         
```

### Custom Application Metrics (python-api)

| Metric | Type | Labels | Description | Alert Use Case |
|--------|------|--------|-------------|----------------|
| `py_api_requests_total` | Counter | `method`, `endpoint`, `status` | Total HTTP requests | 5xx rate spike |
| `py_api_request_duration_seconds` | Histogram | None | Request latency | p95 > 2s |
| `py_api_active_workers` | Gauge | None | Currently running workers | Workers = 0 (dead) |
| `py_api_queue_size` | Gauge | None | Pending jobs (approx) | Queue > 100 (backlog) |
| `py_api_worker_restarts_total` | Counter | None | Restart events | Frequent restarts (OOM?) |
| `py_api_script_executions_total` | Counter | `script_name`, `status` | Executions by file | Specific script failing |
| `py_api_venv_creations_total` | Counter | None | New venv creations | High = cache misses |
| `py_api_venv_cache_hits_total` | Counter | None | Venv reuses | Should be >> creations |

### n8n Metrics

Enabled with `N8N_METRICS=true`. Scraped over the `monitoring` network; `https://$DOMAIN/metrics` is blocked by the Traefik `n8n-metrics` router (403).

| Job | Target | Discovery | Key Metrics |
|-----|--------|-----------|-------------|
| `n8n-main` | `n8n-main:5678` | static | `n8n_scaling_mode_queue_jobs_waiting`, `_active`, `_completed`, `_failed` (needs `N8N_METRICS_INCLUDE_QUEUE_METRICS=true`) |
| `n8n-worker` | `n8n-worker:5678` | `dns_sd_configs` (one target per replica) | Node.js process: `n8n_process_cpu_seconds_total`, `n8n_process_resident_memory_bytes`, `n8n_nodejs_eventloop_lag_seconds` (needs `QUEUE_HEALTH_CHECK_ACTIVE=true`) |

```bash
docker compose exec prometheus wget -qO- http://n8n-main:5678/metrics | grep queue_jobs
curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/metrics   # expect 403
```

### Infrastructure Metrics

| Source | Port | Metric Prefix | Key Metrics |
|--------|------|---------------|-------------|
| **cadvisor** | 8080 | `container_` | `container_cpu_usage_seconds_total`, `container_memory_usage_bytes`, `container_network_receive_bytes_total` |
| **node-exporter** | 9100 | `node_` | `node_cpu_seconds_total`, `node_memory_MemTotal_bytes`, `node_disk_io_time_seconds_total`, `node_network_receive_bytes_total` |

### Grafana Dashboard Reference

**Dashboard:** `grafana_dashboards/python.json`

| Panel ID | Title | PromQL Query | Purpose |
|----------|-------|--------------|---------|
| 1 | Request Rate | `rate(py_api_requests_total[5m])` | Traffic volume by endpoint/status |
| 2 | Request Duration (p95) | `histogram_quantile(0.95, rate(py_api_request_duration_seconds_bucket[5m]))` | Latency SLA |
| 3 | Active Workers | `py_api_active_workers` | Pool health |
| 4 | Worker Restarts | `py_api_worker_restarts_total` | Stability indicator |
| 5 | Script Executions | `rate(py_api_script_executions_total[5m])` | Execution volume by script |
| 6 | Venv Operations | `rate(py_api_venv_creations_total[5m])`, `rate(py_api_venv_cache_hits_total[5m])` | Cache efficiency |
| 7 | Container CPU | `rate(container_cpu_usage_seconds_total{name="python-api"}[5m])` | Resource usage |
| 8 | Container Memory | `container_memory_usage_bytes{name=~"python-api\\|n8n-main\\|redis"}` | Memory pressure |
| 9 | System CPU | `100 - (avg by (instance) (irate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)` | Host CPU % |
| 10 | System Memory | `(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes) / node_memory_MemTotal_bytes * 100` | Host memory % |
| 11 | Scrape Status | `up` | Target health table |