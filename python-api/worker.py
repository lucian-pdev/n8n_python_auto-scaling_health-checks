#!/usr/bin/env python3

from packagemanager import PackageManager
from exceptions import JobRetryingException, VenvCreationException
import subprocess
import json
import datetime as dt
from multiprocessing import Process, Queue
import time
import os
from pathlib import Path
import logging
from typing import Optional, Dict, Any, Tuple

try:
    from main import ACTIVE_WORKERS, WORKER_RESTARTS
except ImportError:
    ACTIVE_WORKERS = None
    WORKER_RESTARTS = None

logger = logging.getLogger(__name__)
WRAPPER_PATH = Path("/app/wrapper.py")


##############################################################
# Task handling
##############################################################

def _validate_task(task: Any) -> Tuple[bool, Optional[str]]:
    """
    Validate task format from job queue.
    Returns (is_valid, signal_or_error).
    """
    if not isinstance(task, (tuple, list)) or len(task) < 1:
        return (False, "invalid_format")
    
    signal = task[0]
    
    if signal in ("STOP", "DRAIN"):
        if len(task) != 4:
            logger.warning(f"Signal {signal} has wrong size: {len(task)}")
        return (True, signal)
    
    if len(task) != 4:
        return (False, "invalid_size")
    
    return (True, "job")


def _unpack_job(task: Tuple) -> Tuple[str, str, str, Dict]:
    """
    Unpack validated job task into components.
    Returns: (job_id, script_name, code, data)
    """
    return task[0], task[1], task[2], task[3]


##############################################################
# Job execution
##############################################################

def _prepare_payload(job_id: str, code: str, data: Dict) -> str:
    """Serialize payload for wrapper stdin."""
    return json.dumps({
        "job_id": job_id,
        "code": code,
        "data": data
    })


def _run_wrapper(
    python_exe: str,
    scripts_dir: Path,
    script_name: str,
    payload: str
) -> subprocess.CompletedProcess:
    """Execute wrapper.py in subprocess with given payload."""
    return subprocess.run(
        [python_exe, str(WRAPPER_PATH)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "PYTHONPATH": str(scripts_dir),
            "SCRIPT_NAME": script_name,
            "PYTHONUNBUFFERED": "1",
        }
    )


def _detect_venv_corruption(error_str: str) -> bool:
    """Check if error indicates venv corruption."""
    corruption_indicators = [
        "No module named",
        "ImportError",
        "ModuleNotFoundError"
    ]
    return any(x in error_str for x in corruption_indicators)


def _execute_job(
    job_id: str,
    script_name: str,
    code: str,
    data: Dict,
    package_manager: PackageManager
) -> Dict[str, Any]:
    """
    Execute a single job with venv resolution and wrapper execution.
    Returns output dict for result_queue.
    """
    start_time = time.time()
    venv_path: Optional[Path] = None
    
    try:
        python_exe, scripts_dir, venv_path = package_manager.prepare_environment(script_name)
        
        payload = _prepare_payload(job_id, code, data)
        result = _run_wrapper(python_exe, scripts_dir, script_name, payload)
        
        process_time = time.time() - start_time
        output = _parse_wrapper_output(result, job_id, process_time)
        
        # Detect and mark venv corruption
        if output.get("status") == "error" and venv_path:
            error_str = str(output.get("error", ""))
            if _detect_venv_corruption(error_str):
                package_manager.mark_venv_corrupted(venv_path)
                output["venv_corrupted"] = True
        
        return output
        
    except subprocess.TimeoutExpired:
        return _build_error_output(
            job_id, "timeout", 
            "Script execution exceeded 60s timeout",
            start_time
        )
        
    except VenvCreationException as e:
        return _build_error_output(
            job_id, "venv_error",
            str(e),
            start_time,
            venv_corrupted=True
        )
        
    except Exception as e:
        logger.exception(f"Unexpected error in worker for job {job_id}")
        return _build_error_output(
            job_id, "error",
            f"Worker internal error: {str(e)}",
            start_time
        )


def _build_error_output(
    job_id: str,
    status: str,
    error: str,
    start_time: float,
    venv_corrupted: bool = False
) -> Dict[str, Any]:
    """Build standardized error output dict."""
    return {
        "job_id": job_id,
        "status": status,
        "error": error,
        "result": None,
        "process_time": time.time() - start_time,
        "venv_corrupted": venv_corrupted
    }


##############################################################
# Output parsing
##############################################################

def _parse_wrapper_output(result: subprocess.CompletedProcess, job_id: str, process_time: float) -> Dict[str, Any]:
    """Parse wrapper.py output with markers."""
    
    if result.returncode != 0:
        return {
            "job_id": job_id,
            "status": "error",
            "error": result.stderr[-2000:] if result.stderr else "Script failed",
            "result": None,
            "process_time": process_time,
            "venv_corrupted": False
        }
    
    stdout = result.stdout
    start_marker = "__RESULT_START__\n"
    end_marker = "\n__RESULT_END__"
    
    try:
        if start_marker in stdout and end_marker in stdout:
            return _extract_json_output(stdout, start_marker, end_marker, job_id, process_time)
        else:
            return _format_missing_markers_output(stdout, job_id, process_time)
            
    except json.JSONDecodeError as e:
        return _format_json_error_output(e, stdout, job_id, process_time)


def _extract_json_output(
    stdout: str,
    start_marker: str,
    end_marker: str,
    job_id: str,
    process_time: float
) -> Dict[str, Any]:
    """Extract and parse JSON from between markers."""
    json_part = stdout.split(start_marker)[1].split(end_marker)[0]
    parsed = json.loads(json_part)
    return {
        "job_id": job_id,
        "status": parsed.get("status", "success"),
        "result": parsed.get("result"),
        "error": parsed.get("error"),
        "process_time": process_time,
        "venv_corrupted": False
    }


def _format_missing_markers_output(stdout: str, job_id: str, process_time: float) -> Dict[str, Any]:
    """Format error when markers not found."""
    return {
        "job_id": job_id,
        "status": "error",
        "error": f"Invalid output format: {stdout[:500]}",
        "result": None,
        "process_time": process_time,
        "venv_corrupted": False
    }


def _format_json_error_output(e: json.JSONDecodeError, stdout: str, job_id: str, process_time: float) -> Dict[str, Any]:
    """Format error when JSON parsing fails."""
    return {
        "job_id": job_id,
        "status": "error",
        "error": f"JSON parse error: {e}",
        "result": stdout[-1000:],
        "process_time": process_time,
        "venv_corrupted": False
    }


##############################################################
# Main worker loop
##############################################################

def worker_main(job_queue: Queue, result_queue: Queue, package_manager: PackageManager):
    """
    Worker process with subprocess isolation.
    Receives pre-loaded code but resolves venv by script_name.
    Uses wrapper.py + stdin.
    Handles venv corruption detection and reporting.
    """
    draining = False
    
    while True:
        task = job_queue.get()
        
        is_valid, signal = _validate_task(task)
        
        if not is_valid:
            logger.error(f"Invalid task: {signal}, type={type(task)}")
            continue
        
        if signal == "STOP":
            break
            
        if signal == "DRAIN":
            draining = True
            continue
        
        # Must be a job
        job_id, script_name, code, data = _unpack_job(task)
        
        output = _execute_job(job_id, script_name, code, data, package_manager)
        result_queue.put(output)
        
        if draining:
            break


##############################################################
# Worker manager
##############################################################

class WorkerManager:
    def __init__(self, num_workers: int = 4):
        self.num_workers = num_workers
        self.job_queue: Queue = Queue()
        self.result_queue: Queue = Queue()
        self.workers: list[Dict] = []
        self._job_assignments: Dict[str, Dict] = {}
        
    def start_workers(self):
        for i in range(self.num_workers):
            pm = PackageManager()
            p = Process(
                target=worker_main,
                args=(self.job_queue, self.result_queue, pm)
            )
            p.start()
            self.workers.append({
                "process": p,
                "birth": dt.datetime.now(dt.timezone.utc).timestamp(),
                "draining": False,
                "worker_id": i
            })
        if ACTIVE_WORKERS:
            ACTIVE_WORKERS.set(len(self.workers))
            
    def stop_workers(self):
        for w in self.workers:
            self.job_queue.put(("STOP", None, None, None))
        for w in self.workers:
            w["process"].join()
        if ACTIVE_WORKERS:
            ACTIVE_WORKERS.set(0)
        
    def _pick_worker(self) -> int:
        return hash(time.time()) % len(self.workers) if self.workers else 0
            
    def submit(self, job_id: str, script_name: str, code: str, data: dict):
        worker_idx = self._pick_worker()
        self._job_assignments[job_id] = {
            "worker_idx": worker_idx,
            "script_name": script_name,
            "code": code,
            "data": data,
            "attempt": 1,
            "submitted_at": time.time()
        }
        self.job_queue.put((job_id, script_name, code, data))
        
    def _handle_worker_death(self, job_id: str, assignment: dict):
        logger.error(f"Worker died for job {job_id}, scheduling retry (attempt {assignment['attempt']})")
        assignment["attempt"] += 1
        self._job_assignments[job_id] = assignment
        self.job_queue.put((
            job_id,
            assignment["script_name"],
            assignment["code"],
            assignment["data"]
        ))
        del self._job_assignments[job_id]
        
    def get_result(self, job_id: str, timeout: float = 60.0):
        deadline = time.time() + timeout
        buffer: list[Dict] = []
        
        try:
            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                
                assignment = self._job_assignments.get(job_id)
                if assignment:
                    worker_idx = assignment["worker_idx"]
                    if 0 <= worker_idx < len(self.workers):
                        if not self.workers[worker_idx]["process"].is_alive():
                            self._handle_worker_death(job_id, assignment)
                            raise JobRetryingException(
                                message="processing taking longer than expected",
                                job_id=job_id,
                                attempt=assignment["attempt"],
                                script_name=assignment["script_name"]
                            )
                
                try:
                    result = self.result_queue.get(timeout=min(remaining, 0.1))
                except Exception:
                    continue
                
                if result.get("job_id") == job_id:
                    self._job_assignments.pop(job_id, None)
                    for r in buffer:
                        self.result_queue.put(r)
                    return result
                else:
                    buffer.append(result)
            
            raise TimeoutError(f"Job {job_id} timed out")
        
        finally:
            for r in buffer:
                self.result_queue.put(r)
            
    def restart_oldest_worker(self):
        if not self.workers:
            return
            
        oldest_idx = min(range(len(self.workers)), 
                        key=lambda i: self.workers[i]["birth"])
        oldest = self.workers[oldest_idx]
        
        if oldest["draining"]:
            return
        
        self.job_queue.put(("DRAIN", None, None, None))
        oldest["process"].join(timeout=60)
        if oldest["process"].is_alive():
            oldest["process"].terminate()
        
        pm = PackageManager()
        p = Process(target=worker_main, args=(self.job_queue, self.result_queue, pm))
        p.start()
        self.workers[oldest_idx] = {
            "process": p,
            "birth": dt.datetime.now(dt.timezone.utc).timestamp(),
            "draining": False
        }
        if WORKER_RESTARTS:
            WORKER_RESTARTS.inc()
        if ACTIVE_WORKERS:
            ACTIVE_WORKERS.set(len(self.workers))