#!/usr/bin/env python3

"""
FastAPI + Uvicorn Python container.
It will receive HTTP requests on the same virtual network, queue them in a workerpool, then return the results to the caller (desgined for n8n).

Expected JSON format:
{
  "data": {{ $json }},
  "code_file_name": "script_name.py"
}

Returns: JSON over HTTP

"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
import time
import uvicorn
from multiprocessing import Process, Queue
from pydantic import BaseModel
from typing import Dict, Any
import datetime as dt
import uuid
from contextlib import asynccontextmanager
import asyncio
import os
# Prometheus metrics
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST # type: ignore


##############################################################
# Configuration
##############################################################
SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", "/app/scripts")


##############################################################
# Prometheus Metrics
##############################################################
REQUEST_COUNT = Counter('py_api_requests_total', 'Total requests', ['method', 'endpoint', 'status'])
REQUEST_DURATION = Histogram('py_api_request_duration_seconds', 'Request duration')
ACTIVE_WORKERS = Gauge('py_api_active_workers', 'Number of active worker processes')
QUEUE_SIZE = Gauge('py_api_queue_size', 'Current job queue size')
WORKER_RESTARTS = Counter('py_api_worker_restarts_total', 'Total worker restarts')
SCRIPT_EXECUTIONS = Counter('py_api_script_executions_total', 'Script executions', ['script_name', 'status'])


##############################################################
# Worker handling area
##############################################################
def worker_main(job_queue, result_queue):
    draining = False
    
    while True:
        job_id, data, code = job_queue.get()
        
        if job_id == "STOP":
            break
        elif job_id == "DRAIN":
            draining = True
            continue # Don't execute DRAIN as a job 
        
        local_vars = {"data": data}
        try:
            starttime = dt.datetime.now(dt.timezone.utc).timestamp()
            exec(code, {}, local_vars)
            result = local_vars.get("result")
            finishtime = dt.datetime.now(dt.timezone.utc).timestamp()
            timetotal = finishtime - starttime
        except Exception as e:
            result = {"error": str(e)}
            timetotal = None
            
        result_queue.put({
            "job_id": job_id, 
            "result": result, 
            "process_time": timetotal,
            "status": "success" if timetotal else "error"
            })
        
        if draining:
            break

class WorkerManager:
    def __init__(self, num_workers=4):
        self.num_workers = num_workers
        self.job_queue = Queue()
        self.result_queue = Queue()
        self.workers = [] # List of (process, birth_timestamp, draining state)
        
    def start_workers(self):
        for _ in range(self.num_workers):
            # self.job_queue.put(("STOP",))
            p = Process(target=worker_main, args=(self.job_queue, self.result_queue))
            p.start()
            self.workers.append({
                "process": p,
                "birth": dt.datetime.now(dt.timezone.utc).timestamp(),
                "draining": False
            })
        ACTIVE_WORKERS.set(len(self.workers))
            
    def stop_workers(self):
        for w in self.workers:
            self.job_queue.put(("STOP", None, None))
        for w in self.workers:
            w["process"].join()
        ACTIVE_WORKERS.set(0)
            
    def submit(self, job_id, data, code):
        self.job_queue.put((job_id, data, code))
        
    def get_result(self, job_id):
        while True:
            result = self.result_queue.get()
            if result["job_id"] == job_id:
                return result
            
    def restart_oldest_worker(self):
        """Mark oldest as draining, replace when done. Simplified: kill and respawn."""
        if not self.workers:
            return
            
        # Find oldest
        oldest_idx = min(range(len(self.workers)), 
                        key=lambda i: self.workers[i]["birth"])
        oldest = self.workers[oldest_idx]
        
        if oldest["draining"] == True:
            return
        
        # Send DRAIN signal - worker exits after current job
        self.job_queue.put(("DRAIN", None, None))
        
        # Wait for worker to finish and exit
        oldest["process"].join(timeout=60)  # Wait up to 60s for current job
        if oldest["process"].is_alive():
            oldest["process"].terminate()  # Force kill if stuck
        
        # Respawn
        p = Process(target=worker_main, args=(self.job_queue, self.result_queue))
        p.start()
        self.workers[oldest_idx] = {
            "process": p,
            "birth": dt.datetime.now(dt.timezone.utc).timestamp(),
            "draining": False
        }
        WORKER_RESTARTS.inc()
        ACTIVE_WORKERS.set(len(self.workers))

# Key DRAIN mechanism:
# Signal	Behavior
# STOP	    Immediate exit, drops current job
# DRAIN	    Sets draining=True, finishes current job, then exits

# Flow:
# restart_oldest_worker()
#  sends ("DRAIN", None, None)
# Worker receives it, sets draining = True
# Worker executes any pending job from queue
# After job completes, if draining: break exits cleanly
# Manager waits join(), then respawns
    
##############################################################
# Script loader area
##############################################################
_script_cache: dict[str, tuple[float, str]] = {}
_script_lock = asyncio.Lock()

async def load_scripts(code_file_name: str) -> str:
    """Load Python scripts from file system with mtime-based cache."""
    name = os.path.basename(code_file_name)
    file_path = os.path.join(SCRIPTS_DIR, name)
    
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail=f"Script not found: {name}")
    
    current_mtime = os.path.getmtime(file_path)
    
    # Fast path: check cache without lock
    # Check cache: hit if name exists and mtime matches
    if name in _script_cache:
        cached_mtime, cached_content = _script_cache[name]
        if cached_mtime == current_mtime:
            return cached_content
        
    # Slow path: acquire lock, re-check, then read
    async with _script_lock:
        # Re-check inside lock (another task may have loaded it)
        if name in _script_cache:
            cached_mtime, cached_content = _script_cache[name]
            if cached_mtime == current_mtime:
                return cached_content
    
        # Read from disk and cache
        with open(file_path, "r") as f:
            content = f.read()
    
        _script_cache[name] = (current_mtime, content)
        return content


##############################################################
# FastAPI area
##############################################################
class ExecPayload(BaseModel):
    data: Dict[str, Any]
    code_file_name: str

manager = WorkerManager(num_workers=4)

@asynccontextmanager
async def lifespan(app: FastAPI):
    manager.start_workers()
    
    # For Prometheus/grafana
    async def metrics_updater():
        while True:
            # Approximate queue size (multiprocessing.Queue doesn't expose size reliably)
            QUEUE_SIZE.set(0)  # Placeholder - would need custom queue for accurate size
            await asyncio.sleep(10)
    
    # BACKGROUND TASK: runs concurrently with FastAPI
    async def restart_loop():
        while True:
            await asyncio.sleep(300)  # 5 minutes
            manager.restart_oldest_worker()
    
 
    
    metrics_task = asyncio.create_task(metrics_updater())
    restart_task = asyncio.create_task(restart_loop())
    
    yield # FastAPI runs here

    # Request cancellation
    metrics_task.cancel()
    restart_task.cancel()
        
    # Shutdown: cancel loop, stop workers
  
    try:
        # Wait for task to actually stop
        await metrics_task
        await restart_task
    except asyncio.CancelledError:
        pass    # Expected when cancelled
    manager.stop_workers()
    

app = FastAPI(lifespan=lifespan)

@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    duration = time.time() - start_time
    
    REQUEST_DURATION.observe(duration)
    REQUEST_COUNT.labels(
        method=request.method,
        endpoint=request.url.path,
        status=response.status_code
    ).inc()
    
    return response

@app.post("/execute")
async def execute(payload: ExecPayload):
    #  Load code from file
    code = await load_scripts(payload.code_file_name)
    
    job_id = str(uuid.uuid4())
    manager.submit(job_id, payload.data, code)
    result = manager.get_result(job_id)
    
    SCRIPT_EXECUTIONS.labels(
        script_name=payload.code_file_name,
        status=result.get("status", "unknown")
    ).inc()
    
    return {"result": result}

##############################################################
# Bonus: /scripts endpoint
##############################################################
# Returns available scripts for n8n dropdown population:
# {
#   "scripts": ["normalize_mobile.py", "validate_email.py", "geocode_address.py"]
# }

@app.get("/scripts")
async def list_scripts():
    """List available scripts for n8n dropdown"""
    try:
        files = [f for f in os.listdir(SCRIPTS_DIR) if f.endswith(".py")]
        return {"scripts": files}
    except FileNotFoundError:
        return {"scripts": []}

##############################################################
# Health Check endpoint area
##############################################################

@app.get("/health")
async def health_check():
    return {"status": "ok", "workers": len(manager.workers)}

@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


##############################################################
# Main area
##############################################################

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)