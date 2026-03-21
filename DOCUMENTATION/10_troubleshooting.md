## Troubleshooting

### Deployment Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `docker: command not found` | Docker not installed | `sudo apt install docker-ce` |
| `permission denied` on scripts | Wrong ownership | `sudo chown -R 1000:1000 scripts/` |
| SSL certificate fails | DNS not propagated | Verify A record, wait for TTL |
| `n8n-data` permission errors | Container UID mismatch | `sudo chown -R 1000:1000 n8n-data/` |

### Service Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| python-api 502 errors | Workers dead | Check `docker compose logs python-api` |
| Slow first execution | Cold venv cache | Normal - subsequent calls fast |
| ImportError in script | Missing `# requires` | Add dependency declaration |
| Grafana no data | Prometheus not scraping | Check `http://VM_IP:9090/targets` all UP |
| Redis connection errors | Redis container down | `docker compose restart redis` |

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

| Service | Command |
|---------|---------|
| python-api | `docker compose logs -f python-api` |
| n8n-main | `docker compose logs -f n8n-main` |
| n8n-worker | `docker compose logs -f n8n-worker` |
| autoscaler | `sudo journalctl -u autoscaler.service -f` |
| nginx health | `sudo tail -f /var/log/nginx_health.log` |
| github sync | `sudo journalctl -u github_sync.service -f` |
| nginx | `sudo tail -f /var/log/nginx/access.log` |

### Web Browser Test URLs

Replace http://192.168.1.100/ (Local LAN) OR http://127.0.0.1/ (host machine) accordingly.

Primary Services (via Nginx - Port 80)
Service	                URL	                  Description
n8n Dashboard	http://192.168.1.100/	        Main n8n workflow automation interface
Grafana	        http://192.168.1.100/grafana/   Monitoring dashboards and metrics visualization
Health Check	http://192.168.1.100/healthz	Returns "healthy" if Nginx is working

Direct Service Access (Bypassing Nginx)
Service	          URL	                      Credentials	            Description
n8n Direct	http://127.0.0.1:5678/	        admin / pass (default)	Direct access to n8n (bypasses nginx)
Grafana Direct	http://127.0.0.1:3000/	    admin / admin (default)	Direct access to Grafana
Python API	http://127.0.0.1:8000/	        N/A	                    FastAPI Python execution service
Prometheus	http://127.0.0.1:9090/	        N/A	                    Metrics collection and querying
cAdvisor	  http://127.0.0.1:8080/	    N/A	                    Container metrics and resource usage

Python API Endpoints (for testing)
Endpoint	        URL	                        Method	    Purpose
Health Check	http://127.0.0.1:8000/health	GET	        Check Python API status
List Scripts	http://127.0.0.1:8000/scripts	GET	        See available Python scripts
Metrics	      http://127.0.0.1:8000/metrics	    GET	        Prometheus-format metrics
Execute Script	http://127.0.0.1:8000/execute	POST	    Execute a Python script