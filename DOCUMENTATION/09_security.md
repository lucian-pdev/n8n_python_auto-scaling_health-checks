## Security Considerations

| Layer | Mitigation |
|-------|------------|
| **Network** | UFW firewall (22, 80, 443 only), Docker bridge networks isolate services |
| **Transport** | TLS 1.2+ via Let's Encrypt, HTTP/2 enabled |
| **Authentication** | n8n Basic Auth, Grafana Basic Auth, python-api assumes private network |
| **Execution** | Subprocess isolation, 60s timeout, no root in containers |
| **Scripts** | Read-only mount (`:ro`), venv isolation, no network restrictions |
| **Secrets** | `.env` file (not in git), N8N_ENCRYPTION_KEY for credentials |
| **Venv** | Corruption detection, auto-purge, system_site_packages=True (limited isolation) |

**Known Limitations:**
- python-api has no authentication (relies on network isolation)
- User scripts can make network calls (no egress filtering)
- Venvs share system site packages (base packages accessible)