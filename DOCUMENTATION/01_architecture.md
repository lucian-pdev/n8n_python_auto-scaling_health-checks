# Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [System Components](#system-components)
3. [Data Flow](#data-flow)
4. [API Reference and Python Script Requirements](#api-reference-and-python-script-requirements)
5. [Metrics & Monitoring](#metrics--monitoring)
6. [Deployment Guide](#deployment-guide)
7. [Autoscaling & Health Monitoring](#autoscaling--health-monitoring)
8. [n8n Integration](#n8n-integration)
9. [Security Considerations](#security-considerations)
10. [Troubleshooting](#troubleshooting)
11. [Maintenance](#maintenance)

---

## Architecture Overview

```
┌─────────────────┐     HTTPS      ┌─────────────────────────────────────────┐
│   Web Client    │ ◄────────────► │          Traefik (container)            │
│  (n8n.cloud)    │                │         :80 → :443 redirect             │
└─────────────────┘                │    TLS termination (Let's Encrypt)      │
                                   └─────────────────────────────────────────┘
                                                    │ proxy network
                          ┌─────────────────────────┼─────────────────────────┐
                          │ Host($DOMAIN)           │ /grafana                │ localhost only
                          ▼                         ▼                         ▼
                   ┌─────────────┐          ┌─────────────┐          ┌─────────────┐
                   │  n8n-main   │          │   Grafana   │          │  Traefik    │
                   │   :5678     │          │   :3000     │          │  dashboard  │
                   │             │          │             │          │ 127.0.0.1:  │
                   │             │          │             │          │    8082     │
                   └──────┬──────┘          └─────────────┘          └─────────────┘
                          │
                          │
                          ┌─────────────────────────────┐
                          │                             │                         
                          |                             | 
                          ▼ Redis Queue (Bull)          ▼
                   ┌─────────────┐               ┌─────────────┐
                   │    redis    │               │  PostGreSQL │
                   │ TASK queue  │               │  TASK data  │
                   │    :6379    │               │             │
                   └──────┬──────┘               └──────┬──────┘
                          ┼─────────────────────────────┘
                          ▼
                   ┌─────────────┐
                   │ n8n-worker  │◄── Autoscaler (systemd, every 30s)
                   │  (scaled)   │    Scales 1-10 based on queue depth
                   └──────┬──────┘
                          │
                          ▼ HTTP POST
                   ┌─────────────┐
                   │  python-api │◄── Prometheus (scrape :8000/metrics)
                   │:8000 FastAPI│◄── Grafana dashboards
                   └──────┬──────┘    │
                          │           │
                   ┌──────┴──────┐    │
                   │  Py Worker  │    │
                   │   Pool (4)  │    │
                   └──────┬──────┘    │
                          │           │
                   ┌──────┴──────┐    │
                   │  Package    │    │
                   │   Manager   │    │
                   │ (venv per   │    │
                   │  script)    │    │
                   └──────┬──────┘    │
                          │           │
                   ┌──────┴──────┐    │
                   │   wrapper   │    │
                   │  (subprocess│    │
                   │   isolate)  │    │
                   └──────┬──────┘    │
                          │           │
                   ┌──────┴──────┐    │
                   │   User      │    │
                   │   Script    │    │
                   │  (/scripts) │────┘
                   └─────────────┘

Monitoring Stack (monitoring network):
├── Prometheus (:9090) ← cadvisor (:8080), node-exporter (:9100), traefik (:8080)
└── Grafana (:3000)
```

Data Persistence Layer (n8n-network):
├── PostgreSQL (Port 5432)
│   ├── Stores n8n Workflow Definitions
│   ├── Stores Execution History & Logs
│   ├── Stores User Credentials & Secrets
│   └── Stores Workflow Variables
└── n8n-data (Local Volume)
    ├── Stores Node State
    └── Stores Local Cache

**Key Design Decisions:**
- **Queue-based execution**: n8n runs in `EXECUTIONS_MODE=queue` with Redis as broker, enabling horizontal scaling of workers
- **Process isolation**: Each Python script runs in separate subprocess with dedicated venv
- **Venv caching**: Virtual environments are reused across scripts with identical requirements (SHA256 hash-based)
- **Worker restart cycle**: Workers restart every 5 minutes via DRAIN signal to prevent memory leaks
- **Autoscaled workers**: n8n-worker containers scale 1-10 based on the n8n job queue in Redis (`bull:jobs:wait`)
- **Security layers**: Firewall (ufw), Traefik reverse proxy (only `traefik.enable=true` containers exposed), Let's Encrypt TLS, isolated Docker networks, subprocess sandboxing