## Maintenance

### Worker Restart Cycle

Workers automatically restart every 5 minutes via `restart_loop()` to prevent memory leaks. Uses `DRAIN` signal for graceful handoff.

**Manual restart:**
```bash
docker compose restart python-api
```

### Script Updates

Scripts are cached by modification time. To force reload:

1. Modify script in `./scripts/`
2. New mtime detected on next request
3. New content loaded and cached

**Or use GitHub sync:**
```bash
sudo systemctl start github_sync.service
```

### TLS Certificate Renewal

Traefik renews Let's Encrypt certs automatically (stored in volume `traefik-letsencrypt` at `/letsencrypt/acme.json`). Check with:
```bash
docker compose logs traefik | grep -i -E "acme|certificate"
```

### Venv Cleanup

Remove old venvs manually if needed:
```bash
docker compose exec python-api python -c "
from packagemanager import PackageManager
pm = PackageManager()
pm._cleanup_old_venvs(max_age_days=1)
"
```

### Backup Strategy

| Data | Location | Backup Method |
|------|----------|---------------|
| n8n workflows | `./n8n-data/` | `tar czf backup-n8n-$(date +%Y%m%d).tar.gz n8n-data/` |
| scripts | `./scripts/` or GitHub repo | `git push` or `tar czf scripts.tar.gz scripts/` |
| grafana dashboards | `./grafana-data/` | Export via UI or volume backup |
| prometheus data | `./prometheus-data/` | Optional (metrics ephemeral) |
| config & secrets | `./.env` | Copy off-host (holds N8N_ENCRYPTION_KEY) |
| TLS certs | volume `traefik-letsencrypt` | Optional (re-issued automatically) |

---

**End of README**