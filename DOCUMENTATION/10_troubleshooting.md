## Troubleshooting

### Deployment Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `docker: command not found` | Docker not installed | `sudo apt install docker-ce` |
| `permission denied` on scripts | Wrong ownership | `sudo chown 1000:1000 scripts/` |
| Certificate not issued | DNS not propagated | Verify A record, wait for TTL, `docker compose logs traefik` |
| `env-maker.sh`: `.env already exists` | Protects N8N_ENCRYPTION_KEY | Edit `.env` directly, or back it up and rerun with `--force` |
| `n8n-data` permission errors | Container UID mismatch | `sudo chown -R 1000:1000 n8n-data/` |
| `n8n` database connection refused | PostgreSQL not ready | `docker compose logs postgres` |
| Permission denied in python-api | Container user mismatch | Check: container runs as root, scripts 644, mount :ro |

### Service Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| python-api 502 errors | Workers dead | Check `docker compose logs python-api` |
| Slow first execution | Cold venv cache | Normal - subsequent calls fast |
| ImportError in script | Missing `# requires` | Add dependency declaration |
| n8n run green but script didn't work | HTTP 200 with `result.status=error` | Add If `result.status == success` + Stop and Error (08_n8n_integration.md); read `result.error` / `result.traceback` |
| Grafana no data | Prometheus not scraping | Check `http://VM_IP:9090/targets` all UP |
| Redis connection errors | Redis container down | `docker compose restart redis` |
| n8n workflows not loading | DB sync pending | Check `docker compose logs n8n-main` |
| n8n credentials missing | Secrets not synced | Check `docker compose logs n8n-worker` |

### Scaling Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Queue growing, no scale | Autoscaler disabled | `sudo systemctl enable --now autoscaler.timer` |
| Workers not scaling down | Jobs still waiting/active | Check `redis-cli LLEN bull:jobs:wait` / `bull:jobs:active`; scale-down needs 0 waiting and active jobs fitting in one fewer worker |
| Worker OOM killed | Script memory leak | Check `docker stats`, restart workers |

### Traefik Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `404 page not found` | No router matched: `traefik.enable=true` label missing, or request Host ≠ `DOMAIN` | Check labels in docker-compose.yml, `DOMAIN` in `.env`; dashboard → HTTP Routers |
| `502 Bad Gateway` | Backend down, not on `proxy` network, or wrong `loadbalancer.server.port` | `docker compose ps`; `docker inspect <svc> --format '{{json .NetworkSettings.Networks}}'` |
| Browser shows `TRAEFIK DEFAULT CERT` | ACME failed: no DNS A record, port 80 blocked, random `.localhost` domain, or LE rate limit | `docker compose logs traefik \| grep -i acme`; test with staging `ACME_CA_SERVER` |
| `bind: address already in use` on :80/:443 | Old host nginx still running | `sudo systemctl disable --now nginx` then `docker compose up -d traefik` |
| Traefik `unhealthy` | Ping check failing | `docker compose logs traefik`, `docker compose restart traefik` |

### Log Locations

| Service | Command | Description |
|---------|---------|-------------|
| python-api | `docker compose logs -f python-api` | FastAPI logs, script execution errors |
| n8n-main | `docker compose logs -f n8n-main` | Workflow sync, DB connection, queue events |
| n8n-worker | `docker compose logs -f n8n-worker` | Execution logs, Redis queue processing |
| autoscaler | `sudo journalctl -u autoscaler.service -f` | Scaling decisions, queue depth metrics |
| github sync | `sudo journalctl -u github_sync.service -f` | Repository sync events |
| traefik | `docker compose logs -f traefik` | Access log (requests, status codes), routing, ACME/cert events |
| postgres | `docker compose logs -f postgres` | DB queries, connection errors, replication |
| redis | `docker compose logs -f redis` | Queue events, memory usage, persistence |
| node-exporter | `sudo tail -f /var/log/syslog \| grep node_exporter` | Host resource usage |
| prometheus | `sudo tail -f /var/log/syslog \| grep prometheus` | Scrape errors, target status |

### Web Browser Test URLs

Replace `$DOMAIN` with the value in `.env`. Traefik routes by Host, so a bare IP returns 404.

Primary Services (via Traefik - https://$DOMAIN)
| Service	| URL | Description |
|---------|---------|-------------|
| n8n Dashboard | https://$DOMAIN/	| Main n8n workflow automation interface |
| Grafana | https://$DOMAIN/grafana/ | Monitoring dashboards and metrics visualization |
| Traefik Dashboard | http://127.0.0.1:8082/dashboard/ | Routers, services, cert status (`ssh -L 8082:127.0.0.1:8082 user@VM`) |

Direct Service Access (Bypassing Traefik)

| Service | URL | Credentials | Description |
|---------|-----|-------------|-------------|
| n8n Direct | http://127.0.0.1:5678/ | `N8N_BASIC_AUTH_*` in `.env` | Direct access to n8n (bypasses Traefik) |
| Grafana Direct | http://127.0.0.1:3000/ | `GF_SECURITY_ADMIN_*` in `.env` | Direct access to Grafana |
| Python API | http://127.0.0.1:8000/ | N/A | FastAPI Python execution service |
| Prometheus | http://127.0.0.1:9090/ | N/A | Metrics collection and querying |
| cAdvisor | http://127.0.0.1:8080/ | N/A | Container metrics and resource usage |

Python API Endpoints (for testing)

| Endpoint | URL | Method | Purpose |
|----------|-----|--------|---------|
| Health Check | http://127.0.0.1:8000/health | GET | Check Python API status |
| List Scripts | http://127.0.0.1:8000/scripts | GET | See available Python scripts |
| Metrics | http://127.0.0.1:8000/metrics | GET | Prometheus-format metrics |
| Execute Script | http://127.0.0.1:8000/execute | POST | Execute a Python script |

### Trace & Diagnostic Methods

#### 1. Docker Inspect (Deep Dive)
```bash
# Inspect container for environment variables and mounts
docker inspect n8n-main --format '{{json .Config.Env}}'
docker inspect n8n-main --format '{{json .Mounts}}'

# Inspect for network connections
docker inspect n8n-main --format '{{json .NetworkSettings.Networks}}'
```

#### 2. Prometheus Metrics Querying
Use `http://VM_IP:9090/graph` to query specific metrics:
- `n8n_queue_length`: Current queue depth
- `n8n_active_executions`: Number of running workers
- `python_api_execution_duration_seconds`: Script execution time
- `node_cpu_seconds_total`: Host CPU usage per core

#### 3. n8n Internal Logs
- **Workflow Execution Logs**: Check `n8n-main` logs for "Workflow executed" events.
- **Credential Errors**: Look for "Invalid credentials" in `n8n-worker` logs.
- **Queue Events**: Look for "Job added" or "Job processed" in `n8n-worker` logs.

#### 4. PostgreSQL Querying
Access the database directly to trace workflow states:
```bash
docker exec -it n8n-main psql -U postgres -d n8n -c "SELECT * FROM \"executions\" ORDER BY \"createdAt\" DESC LIMIT 10;"
```

#### 5. Redis Key Inspection
Check Redis keys directly:
```bash
docker exec -it redis redis-cli keys "*n8n*"
docker exec -it redis redis-cli get "n8n:queue:default:pending"
```

#### 6. System Resource Monitoring
- **CPU/Memory**: `docker stats --no-stream`
- **Disk I/O**: `iostat -x 1`
- **Network**: `iftop` or `nethogs`

#### 7. Traefik Access Logs (Detailed)
```bash
# Filter by status code / path
docker compose logs traefik | grep '" 502 '
docker compose logs traefik | grep "POST /webhook"

# Check for slow requests (> 5s; last field is duration, e.g. 6123ms)
docker compose logs --no-log-prefix traefik | awk '$NF ~ /ms$/ && $NF+0 > 5000'
```

#### 8. Script Ownership Verification
```bash
# Check directory vs file ownership (should be split)
ls -la scripts/          # dir: vagrant:vagrant, files: root:root
stat -c "%U:%G" scripts/ # 1000:1000 expected for directory

# Verify container can read
docker exec python-api ls -la /app/scripts/
docker exec python-api cat /app/scripts/test.py  # Should succeed
```