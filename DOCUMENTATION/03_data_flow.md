## Data Flow

### Webhook-to-Result Lifecycle

| Step | Component | Action | Duration | Data Transform |
|------|-----------|--------|----------|----------------|
| 1 | External System | HTTP POST to `https://n8n.dashboard.com/webhook/PersonCreated` | Network RTT | Raw JSON payload |
| 2 | nginx | SSL termination, route to localhost:5678 | <1ms | None |
| 3 | n8n-main | Receive webhook, trigger workflow, queue execution | ~50ms | Parse to n8n nodes |
| 4 | redis | Store job in Bull queue (list: `bull:queueName:wait`) | <1ms | Serialized job |
| 5 | n8n-worker | Pick job from queue, process workflow | Variable | Execute n8n nodes |
| 6 | n8n-worker | HTTP Request node → POST to `http://python-api:8000/execute` | Network RTT | JSON: `{data, code_file_name}` |
| 7 | python-api (main) | Receive request, validate Pydantic model, cache lookup | ~5ms | ExecPayload validated |
| 8 | python-api (main) | Submit to WorkerManager, assign job_id (uuid4) | <1ms | Job queued |
| 9 | python-api (worker) | Worker process picks job from multiprocessing.Queue | <1ms | Task tuple unpacked |
| 10 | python-api (PackageManager) | Resolve venv: extract reqs → hash → check exists/create | 0ms (cached) to 30s (new venv) | Venv path resolved |
| 11 | python-api (wrapper) | `subprocess.run()` wrapper.py with payload via stdin | 60s timeout max | Code+data injected |
| 12 | wrapper.py | `exec(code, user_namespace)` with `data` in globals | Script-dependent | User code executes |
| 13 | User Script | Business logic: extract person, normalize phone, title-case names | ms to seconds | `result` defined |
| 14 | wrapper.py | Capture `result`, wrap with markers, JSON to stdout | <1ms | `__RESULT_START__{...}__RESULT_END__` |
| 15 | python-api (worker) | Parse output, detect corruption, build response dict | <1ms | Output dict with status |
| 16 | python-api (main) | Return HTTP 200 (success) or 202 (retrying) | <1ms | JSON response |
| 17 | n8n-worker | Receive result, continue workflow | Variable | n8n node output |
| 18 | n8n-main | Complete execution, log to n8n-data | ~10ms | Execution saved |

**Total Typical Latency:** 100-500ms (warm venv, simple script)

### Worker Signals (python-api internal)

| Signal | Payload | Behavior | Triggered By |
|--------|---------|----------|--------------|
| `STOP` | `(signal, None, None, None)` | Immediate exit, no cleanup | Container shutdown |
| `DRAIN` | `(signal, None, None, None)` | Finish current job, then exit | `restart_oldest_worker()` every 5min |
| `JOB` | `(job_id, script_name, code, data)` | Execute in subprocess, return result | HTTP POST to `/execute` |

### n8n Queue Mode Configuration

| Mode | EXECUTIONS_MODE | Behavior | Use Case |
|------|---------------|----------|----------|
| **Regular** | `regular` | Executions run in main process | Development, single-node |
| **Queue** | `queue` (used) | Main orchestrates, workers execute | **Production, horizontal scale** |

**Queue Benefits:**
- Workers can scale independently (1-10 via autoscaler)
- Main process stays responsive under load
- Failed workers don't lose jobs (Redis persists)
- `OFFLOAD_MANUAL_EXECUTIONS_TO_WORKERS=true` for consistent behavior

### Prometheus Scraping Flow

```
python-api (port 8000/metrics) ──┐
                                 ▼
cadvisor (port 8080/metrics) ──► prometheus (port 9090) ──► grafana (port 3000)
                                 ▲
node-exporter (port 9100) ──────┘
         │
         └── System-level: CPU, memory, disk, network, load
```

**Scrape Interval:** 15 seconds

**Retention:** Default 15 days (Prometheus TSDB)