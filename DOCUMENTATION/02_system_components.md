## System Components

### Core Application Services

| Component | Purpose | Port | Image/Build | Networks | Key Volumes |
|-----------|---------|------|-------------|----------|-------------|
| **n8n-main** | Workflow orchestration & webhook receiver | 5678 | `n8nio/n8n:latest` | n8n-network | n8n-data:/home/node/.n8n |
| **n8n-worker** | Queue job processor (scales 1-10) | None | `n8nio/n8n:latest` | n8n-network | None (stateless) |
| **redis** | Bull queue broker for n8n executions | 6379 | `redis:latest` | n8n-network | redis-data:/data |
| **python-api** | FastAPI Python execution service with venv management | 8000 | Build: `./python-api/Dockerfile` | n8n-network, monitoring | ./scripts:/app/scripts:ro |

> **Note:** n8n-worker has no exposed ports; it communicates via Redis and HTTP to python-api internally.

### Monitoring & Observability

| Component | Purpose | Port | Image | Scrape Targets | Key Metrics |
|-----------|---------|------|-------|----------------|-------------|
| **prometheus** | Metrics collection & storage | 9090 | `prom/prometheus:v3.10.0` | Self, python-api, cadvisor, node-exporter | TSDB with 15s scrape interval |
| **grafana** | Visualization dashboards | 3000 | `grafana/grafana:main-ubuntu` | Prometheus datasource | 11 panels (request rate, latency, workers, venv ops, system resources) |
| **cadvisor** | Container resource metrics | 8080 | `gcr.io/cadvisor/cadvisor:v0.47.2` | N/A (exposes /metrics) | container_cpu, container_memory |
| **node-exporter** | Host system metrics | 9100 | `prom/node-exporter:v1.7.0` | N/A (exposes /metrics) | node_cpu, node_memory, disk, network |

> **Note:** cadvisor runs `privileged: true` for full container visibility.

### Infrastructure & Automation (Host-level)

| Component | Purpose | Location | Trigger | Dependencies |
|-----------|---------|----------|---------|--------------|
| **nginx** | Reverse proxy, SSL termination, routing | `/etc/nginx/sites-available/` | systemd | SSL certs in `/etc/letsencrypt/` |
| **autoscaler** | Dynamic n8n-worker scaling | `/usr/local/bin/autoscaler.sh` | systemd timer (30s) | Prometheus query API, docker compose |
| **nginx_health** | Ensures nginx availability | `/usr/local/bin/nginx_health.sh` | systemd timer (2m) | systemctl, nginx, curl |
| **github_sync** | Sync vetted scripts from GitHub | `/usr/local/bin/github_sync.sh` | systemd timer (5min default) | git, GitHub repo access |
| **firewall_rules** | UFW configuration | `./HTTPS/firewall_rules.sh` | Manual/auto-deploy | ufw |

### Python Script Execution Stack (Inside python-api container)

| Layer | File | Responsibility | Isolation Mechanism |
|-------|------|----------------|---------------------|
| HTTP API | `main.py` | FastAPI endpoints, metrics, lifespan mgmt | None (main process) |
| Worker Manager | `worker.py` | 4-worker multiprocessing pool, job routing | Process pool |
| Package Manager | `packagemanager.py` | Venv creation, caching, corruption handling | Filesystem (per-script venv) |
| Subprocess Wrapper | `wrapper.py` | Code execution with data injection | Subprocess + namespace |
| User Script | `./scripts/*.py` | Business logic (phone normalization, etc.) | globals() isolation |

### Virtual Environment Deep Structure

```
/app/venvs/                    # VENV_BASE
└── {hash16}/                  # SHA256 of sorted requirements
    ├── bin/
    │   ├── python → /usr/local/bin/python3.12  # System Python symlink
    │   ├── pip                                # Venv-specific pip
    │   └── activate                           # Unused in containers
    ├── include/                               # C headers
    ├── lib/python3.12/site-packages/          # Installed packages
    ├── lib64 → lib/                           # Symlink
    ├── pyvenv.cfg                             # Venv config
    └── .requirements                          # Persisted requirements list
```

**Venv Naming:** `hashlib.sha256("|".join(sorted(reqs))).hexdigest()[:16]`

**Reuse Strategy:** Scripts with identical requirements share venvs regardless of filename.