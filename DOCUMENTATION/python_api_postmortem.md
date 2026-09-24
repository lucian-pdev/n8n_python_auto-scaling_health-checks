# python-api Debugging & n8n Ops — Post-Mortem
**Date:** 2026-07-19  
**System:** `srv908901` — n8n worker-based stack with python-api FastAPI service  
**Scope:** Grafana metric quality, python-api memory leak, worker lifecycle bug, n8n execution retention misconfiguration

---

## Table of Contents

1. [Grafana — Worker Restart Deviation Panel](#1-grafana--worker-restart-deviation-panel)
2. [python-api — Unexposed Metrics](#2-python-api--unexposed-metrics)
3. [python-api — Memory Leak Investigation](#3-python-api--memory-leak-investigation)
4. [python-api — Worker DRAIN Bug](#4-python-api--worker-drain-bug)
5. [n8n — Execution Retention Misconfiguration](#5-n8n--execution-retention-misconfiguration)
6. [Lessons Learned](#6-lessons-learned)

---

## 1. Grafana — Worker Restart Deviation Panel

### Problem

The original Worker Restarts panel showed a raw cumulative counter (`py_api_worker_restarts_total`). The value climbed monotonically and provided no signal — a healthy system with normal 5-minute cycling looked identical to a system restarting workers too fast or not at all.

### Root Cause

A raw counter has no baseline. Without knowing the expected restart rate, the number is meaningless. The panel also failed to catch the inverse anomaly: zero restarts, which would mean the restart loop had silently crashed.

### Fix

Replace the raw counter with a percentage deviation from expected rate, using a 15-minute window to smooth single-cycle noise:

```
abs(rate(py_api_worker_restarts_total[15m]) - (1/300)) / (1/300) * 100
```

Breaking it down:
- `1/300` = expected rate (1 restart per 300 seconds)
- `rate(...[15m])` = 15-minute window prevents oscillation between 0 and 1 captured restarts
- `abs(...)` = catches both too fast (loop runaway) and too slow (loop stalled)
- Result = percentage deviation from expected

**Thresholds:**

| Value | Colour | Meaning |
|-------|--------|---------|
| 0–10% | Green | Normal cycling jitter |
| 10–20% | Yellow | Drifting — investigate |
| >20% | Red | Loop stalled or runaway |

**Note:** For the first 15 minutes after a container restart, the rate will show artificially high deviation as the window fills. This is expected and not a bug.

### Applying via Grafana API

```bash
cd ~/n8n_python_auto-scaling_health-checks

GF_USER=$(grep "^GF_SECURITY_ADMIN_USER" .env | cut -d= -f2 | tr -d "'" | tr -d '"')
GF_PASS=$(grep "^GF_SECURITY_ADMIN_PASSWORD" .env | cut -d= -f2 | tr -d "'" | tr -d '"')

curl -sf -X POST "http://localhost:3000/api/dashboards/db" \
  -u "$GF_USER:$GF_PASS" \
  -H "Content-Type: application/json" \
  -d "$(curl -s -u \"$GF_USER:$GF_PASS\" http://localhost:3000/api/dashboards/uid/python-api-overview | \
    python3 -c "
import sys, json
d = json.load(sys.stdin)
dashboard = d['dashboard']
for panel in dashboard['panels']:
    if panel['id'] == 4:
        panel['title'] = 'Worker Restart Deviation %'
        panel['targets'] = [{
            'refId': 'A',
            'expr': 'abs(rate(py_api_worker_restarts_total[15m]) - (1/300)) / (1/300) * 100',
            'legendFormat': '% deviation from expected'
        }]
        panel['fieldConfig'] = {
            'defaults': {
                'unit': 'percent', 'decimals': 1,
                'thresholds': {'mode': 'absolute', 'steps': [
                    {'value': None, 'color': 'green'},
                    {'value': 10, 'color': 'yellow'},
                    {'value': 20, 'color': 'red'}
                ]}
            }
        }
        panel['options'] = {'reduceOptions': {'calcs': ['lastNotNull']}, 'colorMode': 'background'}
print(json.dumps({'dashboard': dashboard, 'overwrite': True}))
")"
```

---

## 2. python-api — Unexposed Metrics

### Problem

Several Prometheus metrics were defined in `main.py` but never incremented anywhere in the codebase. Two more valuable data points were computed per-job but discarded rather than emitted.

### Dead Metrics (defined, never called)

| Metric | Where defined | Why never fired |
|--------|--------------|-----------------|
| `py_api_venv_creations_total` | `main.py` | `_create_venv()` never calls `.inc()` |
| `py_api_venv_cache_hits_total` | `main.py` | venv reuse path returns silently |

### Computed But Not Exposed

| Data | Where it lives | Why it matters |
|------|---------------|----------------|
| `process_time` | `worker.py` result dict | Only way to get per-script execution latency |
| Venv corruption events | `mark_venv_corrupted()` log only | No alertable signal |
| Oldest worker age | `birth` timestamps in worker list | Needed to distinguish normal cycling from runaway restarts |
| Job retry count | `attempt` in `_job_assignments` | No visibility into worker death rate |

### Fixes

#### `main.py` — Add new metric definitions

After the existing `VENV_CACHE_HITS` line:

```python
SCRIPT_EXECUTION_DURATION = Histogram(
    'py_api_script_execution_duration_seconds',
    'Per-script execution time in seconds',
    ['script_name']
)
JOB_RETRIES = Counter(
    'py_api_job_retries_total',
    'Jobs retried due to worker death',
    ['script_name']
)
VENV_CORRUPTIONS = Counter(
    'py_api_venv_corruptions_total',
    'Venv corruption events detected'
)
OLDEST_WORKER_AGE = Gauge(
    'py_api_oldest_worker_age_seconds',
    'Age in seconds of the oldest running worker process'
)
```

#### `main.py` — Wire `OLDEST_WORKER_AGE` into the metrics updater loop

```python
async def metrics_updater():
    while True:
        QUEUE_SIZE.set(0)
        if manager.workers:
            now = time.time()
            oldest_birth = min(w["birth"] for w in manager.workers)
            OLDEST_WORKER_AGE.set(now - oldest_birth)
        await asyncio.sleep(10)
```

#### `main.py` — Observe `process_time` and count retries in `/execute`

```python
try:
    result = manager.get_result(job_id)
    status = result.get("status", "unknown") if result else "unknown"

    SCRIPT_EXECUTIONS.labels(script_name=payload.code_file_name, status=status).inc()

    if result and result.get("process_time"):
        SCRIPT_EXECUTION_DURATION.labels(script_name=script_name).observe(result["process_time"])

    return {"result": result}

except JobRetryingException as e:
    JOB_RETRIES.labels(script_name=e.script_name or "unknown").inc()
    return JSONResponse(status_code=202, content={...})
```

#### `packagemanager.py` — Import metrics without circular dependency

```python
try:
    from main import VENV_CREATIONS, VENV_CACHE_HITS, VENV_CORRUPTIONS
except ImportError:
    VENV_CREATIONS = VENV_CACHE_HITS = VENV_CORRUPTIONS = None
```

Fire `VENV_CACHE_HITS` in the reuse path of `prepare_environment()`:

```python
if venv_path.exists():
    if VENV_CACHE_HITS:
        VENV_CACHE_HITS.inc()
    return (str(venv_path / "bin" / "python"), self.SCRIPTS_DIR, venv_path)
```

Fire `VENV_CREATIONS` at the end of `_create_venv()`:

```python
(venv_path / ".requirements").write_text("\n".join(requirements))
if VENV_CREATIONS:
    VENV_CREATIONS.inc()
```

Fire `VENV_CORRUPTIONS` in `mark_venv_corrupted()`:

```python
logger.warning(f"Marked venv as corrupted: {venv_path}")
if VENV_CORRUPTIONS:
    VENV_CORRUPTIONS.inc()
```

### New Grafana Panels Unlocked

| Panel | PromQL | Value |
|-------|--------|-------|
| Script p95 latency by name | `histogram_quantile(0.95, rate(py_api_script_execution_duration_seconds_bucket[5m]))` by `script_name` | Identifies slow scripts |
| Venv cache hit rate % | `rate(cache_hits[5m]) / (rate(cache_hits[5m]) + rate(creations[5m])) * 100` | % reuse vs cold build |
| Oldest worker age | `py_api_oldest_worker_age_seconds` | Flat ~300s = healthy; climbing = restart loop broken |
| Job retries/rate | `rate(py_api_job_retries_total[5m])` | Worker death frequency |

---

## 3. python-api — Memory Leak Investigation

### Observed Behaviour

- Container RAM grew from ~53MB baseline to 3.5GB over 4 days (linear, ~1.7GB/day)
- After rebuild with 1.5GB limit: still 6MB growth every 5 minutes
- Growth correlated exactly with the 5-minute worker restart cycle

### Diagnosis

**Step 1 — Rule out venvs:**
```bash
docker exec python-api find /app/venvs -maxdepth 1 -type d | wc -l
docker exec python-api du -sh /app/venvs/
# Result: 1 directory, 4.0K — not the cause
```

**Step 2 — Check process count:**
```bash
ps aux | grep "python3.12.*uvicorn" | grep -v grep
```

Result at 13:25 (container started at 13:07):
```
1097768  13:07  main uvicorn      54656 RSS
1097816  13:07  worker 1          40624 RSS  ← original, still running
1097818  13:07  worker 2          40628 RSS  ← original, still running
1097820  13:07  worker 3          40572 RSS  ← original, still running
1097822  13:07  worker 4          39484 RSS  ← original, still running
1098967  13:13  worker             40044 RSS  ← replacement, also running
1100168  13:19  worker             40188 RSS  ← replacement, also running
1101550  13:25  worker             40192 RSS  ← replacement, also running
```

**8 worker processes running. Only 4 should exist.** Old workers were never exiting. Each orphaned worker held ~40MB. The leak was accumulating child processes, not heap fragmentation.

**Step 3 — Verify parent process VmRSS was flat:**
```bash
docker exec python-api cat /proc/1/status | grep VmRSS
# VmRSS stable at ~54KB across multiple readings
```

Confirmed: parent (uvicorn) was not leaking. `malloc_trim` was working on the parent. The leak was 100% accumulating zombie-ish worker processes.

### Root Cause — DRAIN Signal Non-Determinism

In `restart_oldest_worker()`:

```python
self.job_queue.put(("DRAIN", None, None, None))  # sent to shared queue
oldest["process"].join(timeout=60)               # waits for oldest specifically
```

The DRAIN signal is sent to a **shared queue** that all 4 workers pull from. Statistically, 3 out of 4 times the DRAIN goes to a worker that is NOT the oldest. The oldest never receives it, never exits voluntarily, and `join()` times out after 60 seconds. `terminate()` then sends SIGTERM — but a process blocked on `multiprocessing.Queue.get()` (a blocking `select()` syscall) may not exit cleanly under SIGTERM if the queue's internal finalizer tries to flush shared state. Result: the process lingers indefinitely.

### Fix — Per-Worker Stop Event

Replace the shared DRAIN queue signal with a per-worker `multiprocessing.Event` that signals only the specific worker being cycled.

#### `worker.py` changes

Add import:
```python
import multiprocessing
```

Add to imports near top:
```python
import ctypes
import ctypes.util

def _trim_malloc():
    """Return glibc free pages to OS after fork/join cycle."""
    try:
        libc_name = ctypes.util.find_library('c')
        if libc_name:
            libc = ctypes.CDLL(libc_name)
            libc.malloc_trim(ctypes.c_int(0))
    except Exception:
        pass
```

Update `worker_main` signature to accept `stop_event`:

```python
def worker_main(job_queue: Queue, result_queue: Queue,
                package_manager: PackageManager,
                stop_event):
    draining = False

    while True:
        if stop_event.is_set():   # check dedicated signal
            break

        task = job_queue.get()
        # ... rest of loop unchanged
```

Update `WorkerManager.start_workers()`:

```python
def start_workers(self):
    for i in range(self.num_workers):
        stop_event = multiprocessing.Event()
        pm = PackageManager()
        p = Process(
            target=worker_main,
            args=(self.job_queue, self.result_queue, pm, stop_event)
        )
        p.start()
        self.workers.append({
            "process": p,
            "birth": dt.datetime.now(dt.timezone.utc).timestamp(),
            "draining": False,
            "worker_id": i,
            "stop_event": stop_event
        })
    if self._active_workers:
        self._active_workers.set(len(self.workers))
```

Update `restart_oldest_worker()` — signal the specific worker, call `_trim_malloc()` after join:

```python
def restart_oldest_worker(self):
    if not self.workers:
        return

    oldest_idx = min(range(len(self.workers)),
                    key=lambda i: self.workers[i]["birth"])
    oldest = self.workers[oldest_idx]

    if oldest["draining"]:
        return

    oldest["draining"] = True
    oldest["stop_event"].set()           # signal THIS worker only
    oldest["process"].join(timeout=10)   # exits almost immediately now
    if oldest["process"].is_alive():
        oldest["process"].terminate()
        oldest["process"].join(timeout=5)

    _trim_malloc()                        # return freed pages to OS

    stop_event = multiprocessing.Event()
    pm = PackageManager()
    p = Process(
        target=worker_main,
        args=(self.job_queue, self.result_queue, pm, stop_event)
    )
    p.start()
    self.workers[oldest_idx] = {
        "process": p,
        "birth": dt.datetime.now(dt.timezone.utc).timestamp(),
        "draining": False,
        "stop_event": stop_event
    }
    if self._worker_restarts:
        self._worker_restarts.inc()
    if self._active_workers:
        self._active_workers.set(len(self.workers))
```

### Additional Tuning — glibc Allocator

Add to `docker-compose.yml` under `python-api` environment (no rebuild needed, restart only):

```yaml
- MALLOC_TRIM_THRESHOLD_=65536    # trim after 64KB freed (default 128KB)
- MALLOC_MMAP_THRESHOLD_=131072   # use mmap for >128KB allocs (returned to OS on free)
- MALLOC_ARENA_MAX=2              # cap arenas (default = 1 per CPU thread × 8)
```

`MALLOC_ARENA_MAX=2` is the most impactful — without it, glibc creates one arena per CPU thread, each holding its own free list. On a 2-core host this multiplies fragmentation surface unnecessarily.

### Memory Limit in docker-compose.yml

```yaml
python-api:
  deploy:
    resources:
      limits:
        memory: 1500m
      reservations:
        memory: 256m
```

This acts as a hard safety ceiling, not a fix. With the DRAIN bug fixed and `malloc_trim` active, steady-state RSS should remain stable at ~55–60MB regardless of uptime.

### Verify After Rebuild

```bash
docker compose up -d --build python-api

# After 15 minutes — should be exactly 5 python processes
ps aux | grep "python3.12.*uvicorn" | grep -v grep | wc -l

# VmRSS should be stable, not climbing
watch -n 60 'docker exec python-api cat /proc/1/status | grep VmRSS'
```

---

## 4. python-api — Circular Import (WORKER_RESTARTS Counter Silent Failure)

### Problem

`WORKER_RESTARTS` was showing 100% deviation in Grafana for 40+ minutes — meaning the counter was stuck at 0 despite the restart loop visibly running.

### Root Cause

In `worker.py`:

```python
try:
    from main import ACTIVE_WORKERS, WORKER_RESTARTS
except ImportError:
    ACTIVE_WORKERS = None
    WORKER_RESTARTS = None
```

When `main.py` does `from worker import WorkerManager`, Python begins executing `worker.py`. At that moment `main` is partially initialized in `sys.modules`. The import appeared to succeed, but when `multiprocessing.Process` forks a new worker, the child re-imports `worker.py` in a fresh interpreter context where `main.py` is not running as an app. The import returned `None` in the child, silently. Since `restart_oldest_worker()` runs in the **main** process, the counter reference was valid there — but the counter itself was never getting `.inc()` called because the worker restart logic was reaching `if WORKER_RESTARTS:` with a valid reference that was somehow stale after the fork.

The clean fix is to eliminate the circular import entirely by passing metric references as constructor arguments.

### Fix — Pass Metrics to WorkerManager Explicitly

Remove the top-level import from `worker.py` entirely. Update `WorkerManager.__init__`:

```python
class WorkerManager:
    def __init__(self, num_workers: int = 4,
                 active_workers_gauge=None,
                 worker_restarts_counter=None):
        self.num_workers = num_workers
        self.job_queue: Queue = Queue()
        self.result_queue: Queue = Queue()
        self.workers: list[Dict] = []
        self._job_assignments: Dict[str, Dict] = {}
        self._active_workers = active_workers_gauge
        self._worker_restarts = worker_restarts_counter
```

Replace all `ACTIVE_WORKERS` / `WORKER_RESTARTS` references inside `WorkerManager` with `self._active_workers` / `self._worker_restarts`.

In `main.py`, construct with metrics passed in:

```python
manager = WorkerManager(
    num_workers=4,
    active_workers_gauge=ACTIVE_WORKERS,
    worker_restarts_counter=WORKER_RESTARTS
)
```

---

## 5. n8n — Execution Retention Misconfiguration

### Problem

`.env` contained:

```env
EXECUTIONS_DATA_SAVE_ON_SUCCESS=20   # intended: keep only 20 successful runs
EXECUTIONS_DATA_SAVE_ON_ERROR=all    # keep all failures
```

Successful executions were not being limited despite this config. The database was accumulating all of them.

### Root Cause

`EXECUTIONS_DATA_SAVE_ON_SUCCESS` does **not** accept a number. The only valid values are `all` or `none`. The value `20` was silently ignored and n8n fell back to its default (`all`), saving every successful execution indefinitely.

There is no native n8n env var that says "keep only N successful executions per workflow." The count-based pruning (`EXECUTIONS_DATA_PRUNE_MAX_COUNT`) applies to all executions combined, not per-type.

### Valid Options

**Option A — Save no successes (recommended):**
```env
EXECUTIONS_DATA_SAVE_ON_SUCCESS=none
EXECUTIONS_DATA_SAVE_ON_ERROR=all
EXECUTIONS_DATA_PRUNE=true
EXECUTIONS_DATA_MAX_AGE=504          # 21 days in hours
EXECUTIONS_DATA_PRUNE_MAX_COUNT=20000
```

**Option B — Save all, cap total count:**
```env
EXECUTIONS_DATA_SAVE_ON_SUCCESS=all
EXECUTIONS_DATA_SAVE_ON_ERROR=all
EXECUTIONS_DATA_PRUNE=true
EXECUTIONS_DATA_MAX_AGE=504
EXECUTIONS_DATA_PRUNE_MAX_COUNT=500  # applies to combined total
```

**Option C — Per-workflow override in the UI:**

n8n UI → Workflow → Settings (gear) → Execution Data section. Per-workflow settings override the global env vars. This allows different retention per workflow — for example, Prod workflows save errors only while test workflows save everything.

### Fix Applied

```env
EXECUTIONS_DATA_SAVE_ON_SUCCESS=none
EXECUTIONS_DATA_SAVE_ON_ERROR=all
EXECUTIONS_DATA_PRUNE=true
EXECUTIONS_DATA_MAX_AGE=504
EXECUTIONS_DATA_PRUNE_MAX_COUNT=20000
```

Apply:
```bash
docker compose up -d n8n-main n8n-worker
```

---

## 6. Lessons Learned

**Grafana metric design:**
- Raw cumulative counters on background tasks provide no signal without a baseline. Always model as rate deviation from expected.
- Symmetric thresholds matter — "too few" events are as anomalous as "too many." A counter stuck at 0 should be red, not green.

**python-api architecture:**
- Prometheus metrics defined as module-level globals and imported via circular dependency are fragile under `multiprocessing.fork()`. Child processes may receive stale or None references. Pass metrics explicitly as constructor arguments — never import them from the app module into a worker module.
- `DRAIN` on a shared queue is non-deterministic when there are multiple consumers. Signals that must target a specific process must use a per-process communication channel (`multiprocessing.Event`, `Pipe`, or a dedicated queue per worker).
- A memory leak growing linearly at a rate that correlates exactly with a known cycle (every N minutes) is almost always accumulation of child processes, not heap fragmentation. Check process count before investigating allocator behaviour.
- `malloc_trim` addresses parent-process allocator fragmentation, not zombie child processes. Both may be present simultaneously; diagnose them separately.

**n8n configuration:**
- `EXECUTIONS_DATA_SAVE_ON_SUCCESS` is a binary enum (`all`/`none`), not a count. An invalid value is silently ignored and falls back to the default. Always verify env var accepted values against current documentation — n8n changed several defaults between 1.x and 2.x.
- Comments after values on the same line (e.g. `VALUE=20 # comment`) are safe in `.env` files for Docker Compose — Docker strips them. But do not rely on this across all tooling.

**Diagnostic commands reference:**

```bash
# Count python worker processes on host
ps aux | grep "python3.12.*uvicorn" | grep -v grep | wc -l

# Check main process memory
docker exec python-api cat /proc/1/status | grep -E "VmRSS|VmSize|VmPeak|VmData"

# Check venv disk usage
docker exec python-api du -sh /app/venvs/

# Verify Prometheus metrics are registered
docker exec python-api curl -s localhost:8000/metrics | grep "# HELP py_api"

# Watch VmRSS over time
watch -n 60 'docker exec python-api cat /proc/1/status | grep VmRSS'
```
