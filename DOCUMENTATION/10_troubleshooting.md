## Troubleshooting

### Deployment Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `docker: command not found` | Docker not installed | `sudo apt install docker-ce` |
| `permission denied` on scripts | Wrong ownership | `sudo chown -R 1000:1000 scripts/` |
| SSL certificate fails | DNS not propagated | Verify A record, wait for TTL |
| `n8n-data` permission errors | Container UID mismatch | `sudo chown -R 1000:1000 n8n-data/` |
| `n8n` database connection refused | PostgreSQL not ready | `docker compose logs postgres` |

### Service Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| python-api 502 errors | Workers dead | Check `docker compose logs python-api` |
| Slow first execution | Cold venv cache | Normal - subsequent calls fast |
| ImportError in script | Missing `# requires` | Add dependency declaration |
| Grafana no data | Prometheus not scraping | Check `http://VM_IP:9090/targets` all UP |
| Redis connection errors | Redis container down | `docker compose restart redis` |
| n8n workflows not loading | DB sync pending | Check `docker compose logs n8n-main` |
| n8n credentials missing | Secrets not synced | Check `docker compose logs n8n-worker` |

### Scaling Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Queue growing, no scale | Autoscaler disabled | `sudo systemctl enable --now autoscaler.timer` |
| Workers not scaling down | Queue threshold too high | Lower `QUEUE_THRESHOLD_DOWN` |
| Worker OOM killed | Script memory leak | Check `docker stats`, restart workers |

### Nginx Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `502 Bad Gateway` | Backend down | `docker compose ps`, check health |
| `/healthz` 404 | nginx config not loaded | `sudo nginx -t && sudo systemctl reload nginx` |
| SSL expired | Certbot failed | `sudo certbot renew --force-renewal` |

### Log Locations

| Service | Command | Description |
|---------|---------|-------------|
| python-api | `docker compose logs -f python-api` | FastAPI logs, script execution errors |
| n8n-main | `docker compose logs -f n8n-main` | Workflow sync, DB connection, queue events |
| n8n-worker | `docker compose logs -f n8n-worker` | Execution logs, Redis queue processing |
| autoscaler | `sudo journalctl -u autoscaler.service -f` | Scaling decisions, queue depth metrics |
| nginx health | `sudo tail -f /var/log/nginx_health.log` | Health check probe results |
| github sync | `sudo journalctl -u github_sync.service -f` | Repository sync events |
| nginx | `sudo tail -f /var/log/nginx/access.log` | HTTP requests, status codes |
| postgres | `docker compose logs -f postgres` | DB queries, connection errors, replication |
| redis | `docker compose logs -f redis` | Queue events, memory usage, persistence |
| node-exporter | `sudo tail -f /var/log/syslog \| grep node_exporter` | Host resource usage |
| prometheus | `sudo tail -f /var/log/syslog \| grep prometheus` | Scrape errors, target status |

### Web Browser Test URLs

Replace http://192.168.1.100/ (Local LAN) OR http://127.0.0.1/ (host machine) accordingly.

Primary Services (via Nginx - Port 80)
| Service	| URL | Description |
|---------|---------|-------------|
| n8n Dashboard | http://192.168.1.100/	| Main n8n workflow automation interface |
| Grafana | http://192.168.1.100/grafana/ | Monitoring dashboards and metrics visualization |
| Health Check | http://192.168.1.100/healthz | Returns "healthy" if Nginx is working |

Direct Service Access (Bypassing Nginx)

| Service | URL | Credentials | Description |
|---------|-----|-------------|-------------|
| n8n Direct | http://127.0.0.1:5678/ | admin / pass (default) | Direct access to n8n (bypasses nginx) |
| Grafana Direct | http://127.0.0.1:3000/ | admin / admin (default) | Direct access to Grafana |
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

#### 7. Nginx Access Logs (Detailed)
```bash
# Filter by status code
sudo grep "502" /var/log/nginx/access.log
sudo grep "POST /execute" /var/log/nginx/access.log

# Check for slow requests (> 5s)
sudo awk '{print $9, $10}' /var/log/nginx/access.log | awk '$1 > 5 {print}'
```