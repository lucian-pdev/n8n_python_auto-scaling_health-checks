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
│   Web Client    │ ◄────────────► │              Nginx (Host)               │
│  (n8n.cloud)    │                │         :80 → :443 redirect             │
└─────────────────┘                │    SSL termination (Let's Encrypt)      │
                                   └─────────────────────────────────────────┘
                                                    │
                          ┌─────────────────────────┼─────────────────────────┐
                          │                         │                         │
                          ▼                         ▼                         ▼
                   ┌─────────────┐          ┌─────────────┐          ┌─────────────┐
                   │  n8n-main   │          │   Grafana   │          │   Healthz   │
                   │   :5678     │          │   :3000     │          │  endpoint   │
                   └──────┬──────┘          └─────────────┘          └─────────────┘
                          │
                          ▼ Redis Queue (Bull)
                   ┌─────────────┐
                   │    redis    │
                   │    :6379    │
                   └──────┬──────┘
                          │
                          ▼
                   ┌─────────────┐
                   │ n8n-worker  │◄── Autoscaler (systemd, every 30s)
                   │  (scaled)   │    Scales 1-10 based on queue depth
                   └──────┬──────┘
                          │
                          ▼ HTTP POST
                   ┌─────────────┐
                   │  python-api │◄── Prometheus (scrape :8000/metrics)
                   │   :8000     │◄── Grafana dashboards
                   └──────┬──────┘    │
                          │            │
                   ┌──────┴──────┐    │
                   │   Worker    │    │
                   │   Pool (4)  │    │
                   └──────┬──────┘    │
                          │            │
                   ┌──────┴──────┐    │
                   │  Package    │    │
                   │   Manager   │    │
                   │ (venv per   │    │
                   │  script)    │    │
                   └──────┬──────┘    │
                          │            │
                   ┌──────┴──────┐    │
                   │   wrapper   │    │
                   │  (subprocess│    │
                   │   isolate)  │    │
                   └──────┬──────┘    │
                          │            │
                   ┌──────┴──────┐    │
                   │   User      │    │
                   │   Script    │    │
                   │  (/scripts) │────┘
                   └─────────────┘

Monitoring Stack (monitoring network):
├── Prometheus (:9090) ← cadvisor (:8080), node-exporter (:9100)
└── Grafana (:3000)
```

**Key Design Decisions:**
- **Queue-based execution**: n8n runs in `EXECUTIONS_MODE=queue` with Redis as broker, enabling horizontal scaling of workers
- **Process isolation**: Each Python script runs in separate subprocess with dedicated venv
- **Venv caching**: Virtual environments are reused across scripts with identical requirements (SHA256 hash-based)
- **Worker restart cycle**: Workers restart every 5 minutes via DRAIN signal to prevent memory leaks
- **Autoscaled workers**: n8n-worker containers scale 1-10 based on Prometheus metrics
- **Security layers**: Firewall (ufw), nginx reverse proxy, SSL, isolated Docker networks, subprocess sandboxing