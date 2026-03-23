## API Reference and Python Script Requirements

### Authentication

**n8n-main:** Basic Auth (N8N_BASIC_AUTH_USER/PASSWORD)

**python-api:** No authentication (assumes private network)

**Grafana:** Basic Auth (GF_SECURITY_ADMIN_USER/PASSWORD)

---

### `POST /execute`

Execute a Python script with provided data.

**Request:**
```json
{
  "data": { /* any JSON object */ },
  "code_file_name": "script_name.py"
}
```

**Success Response (200):**
```json
{
  "result": {
    "job_id": "uuid",
    "status": "success",
    "result": /* user-defined result */,
    "process_time": 0.123,
    "venv_corrupted": false
  }
}
```

**Retry Response (202):**
```json
{
  "result": null,
  "status": "retrying",
  "message": "processing taking longer than expected",
  "job_id": "uuid",
  "attempt": 2,
  "script_name": "script_name.py"
}
```

**Error Response (500 via wrapper):**
```json
{
  "job_id": "uuid",
  "status": "error", 
  "error": "Traceback ...",
  "result": null,
  "process_time": 0.045,
  "venv_corrupted": false
}
```

---

### Script Requirements Declaration

Scripts can declare additional Python packages in header comments:

```python
# requires: pandas==2.1.4, numpy>=1.24.0, requests

def process_data(data):
    import pandas as pd
    df = pd.DataFrame(data["items"])
    result = df.groupby("category").sum()
    return result

result = process_data(data)
```

**Rules:**
- The **first lines must be commented** and the requirements must be present in those lines
        Note: the requirements checking stops at first non-commented line (#)
- Format: `# requires: package==version, package2`
- Version specifiers: `==`, `>=`, `<=`, `>`, `<` supported
- Base packages (fastapi, uvicorn, pydantic, prometheus-client) are pre-installed and filtered out

---

### Virtual Environment Lifecycle

| State | Action | Performance |
|-------|--------|-------------|
| Cache Hit | Venv exists for exact requirements | Zero overhead |
| Cache Miss | Create venv + `pip install` | 10-60 seconds first time |
| Corruption | Detected via ImportError/ModuleNotFoundError | Auto-purge + recreate on next use |

**Venv Storage:** `/app/venvs/` (Docker volume, persists across restarts)

**Cleanup:** Automatic (corrupted) + manual `_cleanup_old_venvs(max_age_days=7)` available

---

### `GET /health`

Health check for Docker and load balancers.

```json
{
  "status": "ok",
  "workers": 4
}
```

---

### `GET /scripts`

List available Python scripts for n8n dropdown population.

```json
{
  "scripts": ["normalize_mobile.py", "validate_email.py", "geocode_address.py"]
}
```

**n8n Usage:** HTTP Request node → `{{ $json.scripts }}` → Split Out node → Options

---

### `GET /metrics`

Prometheus metrics in text format.

**Custom Metrics Exposed:**

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `py_api_requests_total` | Counter | `method`, `endpoint`, `status` | HTTP request count |
| `py_api_request_duration_seconds` | Histogram | None | Request latency distribution |
| `py_api_active_workers` | Gauge | None | Current worker processes |
| `py_api_queue_size` | Gauge | None | Job queue size (approximate) |
| `py_api_worker_restarts_total` | Counter | None | Worker restart count |
| `py_api_script_executions_total` | Counter | `script_name`, `status` | Executions per script |
| `py_api_venv_creations_total` | Counter | None | New venv creations |
| `py_api_venv_cache_hits_total` | Counter | None | Venv reuse count |

**Example Query:**
```
rate(py_api_script_executions_total{status="success"}[5m])
```

---

### Script Execution Context

**Available in User Scripts:**

| Variable | Value | Purpose |
|----------|-------|---------|
| `data` | Full payload from n8n | Primary input data |
| `_n8n_data` | Alias of `data` | Backward compatibility |
| `__builtins__` | Python builtins | Standard library access |

**Required Output:**

User script **must** define a `result` variable:

```python
# Good: result defined
result = {"processed": len(items)}

# Bad: no result
print("done")  # Output lost
```

**Security Constraints:**
- 60-second execution timeout
- Subprocess isolation (no shared memory with main process)
- No network restrictions (can HTTP call external APIs)
- Filesystem limited to container (tmp writable, scripts read-only)