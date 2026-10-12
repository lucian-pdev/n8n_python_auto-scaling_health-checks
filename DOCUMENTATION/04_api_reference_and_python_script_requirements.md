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

**Error Response (still HTTP 200):**
```json
{
  "result": {
    "job_id": "uuid",
    "status": "error",
    "error": "KeyError: 'email' (line 12 in normalize)",
    "error_type": "KeyError",
    "traceback": "Traceback (most recent call last):\n  File \"my_script.py\", line 12, in normalize\n ...",
    "result": null,
    "process_time": 0.045,
    "venv_corrupted": false
  }
}
```

HTTP 200 means "python-api answered", not "script worked". Always check `result.status` in n8n (see 08_n8n_integration.md -> Error handling).

| `result.status` | Cause | `result.error` |
|---|---|---|
| `success` | script set `result` | - |
| `error` | script raised, didn't set `result`, or `result` not JSON-serializable | one line, see below |
| `timeout` | ran > 60s | `Script execution exceeded 60s timeout` |
| `venv_error` | `# requires` packages couldn't be installed | pip/venv message |
| `retrying` (HTTP 202, top-level `status`) | worker died, job requeued | - (`message`) |

### Error Reporting in Scripts

Done by `wrapper.py` for every script, nothing to import:

| Script does | `result.error` | `result.error_type` |
|---|---|---|
| `raise ScriptError("config needs key_fields")` | `config needs key_fields` (as-is) | `ScriptError` |
| any other exception | `<Type>: <msg> (line N in <func>)` | exception class name |
| syntax error | `SyntaxError: ... (my_script.py, line N)` | `SyntaxError` |
| never sets `result` | `script did not set \`result\`` | `MissingResult` |
| `result = {1, 2}` (set, datetime, object...) | `result is not JSON-serializable: ...` | `ResultNotSerializable` |

- `ScriptError` - injected into the script's namespace. Use it for expected failures (bad input/config); the message reaches n8n unchanged, so make it stand alone: what's wrong + what's allowed.
- A script may define its own `class ScriptError(Exception)` (for local runs without python-api); matched by name, same behaviour.
- `traceback` - script frames only (wrapper frames dropped), with source lines, file = script name. Last 4000 chars.
- Errors are also logged: `docker compose logs python-api | grep "job "`.
- Row-level problems that shouldn't stop a run: put them in your `result` (e.g. `{"report": {"skipped": [...], "warnings": [...]}}`), not in exceptions.

```python
# Pattern: validate input first, fail with a clear message
if not data.get("config", {}).get("key_fields"):
    raise ScriptError("config.key_fields is required, e.g. [\"Email\"]")
```

Example with mode dispatch and per-mode error context: `workflows/sheets_to_airtable/sheets_to_airtable.py` (`@mode`, `@reports_errors`).

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
| `ScriptError` | Exception class | Expected failures with a clean message (see Error Reporting) |
| `__builtins__` | Python builtins | Standard library access |

**Required Output:**

User script **must** define a `result` variable:

```python
# Good: result defined
result = {"processed": len(items)}

# Bad: no result -> status "error", "script did not set `result`"
print("done")
```

**Security Constraints:**
- 60-second execution timeout
- Subprocess isolation (no shared memory with main process)
- No network restrictions (can HTTP call external APIs)
- Filesystem limited to container (tmp writable, scripts read-only)