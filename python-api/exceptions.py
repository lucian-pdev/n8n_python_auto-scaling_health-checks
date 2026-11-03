#!/usr/bin/env python3
"""
Custom exceptions for worker management and job retry logic.

Thought model:
RetryableError is internal (the runner retries it), FatalError is what n8n gets.

JobError                      # base: carries job_id, attempt, script_name, status, to_dict()
├── RetryableError            # status = "retrying"; internal only, counts against MAX_ATTEMPTS
│   ├── JobTimeout            # an attempt (or venv build) ran out of time
│   ├── MemoryCapExceeded
│   └── WorkerCrashed         # replaces WorkerDiedException
└── FatalError                # status = "fatal"; reported to n8n
    ├── JobNotFound           # replaces cKeyError
    ├── VenvCreationException  # needs investigation of packages present and to be installed, not an easy fix
    ├── ScriptError
    └── RetriesExhausted    # retries/time budget ran out; carries the last RetryableError
"""
        
class JobError(Exception):
    """Base for everything the runner reports to n8n."""
    status = "fatal"
    
    def __init__(self, message: str, job_id: str | None = None, 
                attempt: int = 0, script_name: str | None = None):
        super().__init__(message)
        self.message = message
        self.job_id = job_id
        self.attempt = attempt
        self.script_name = script_name
        
    def to_dict(self) -> dict:
        return {
        "status": self.status,
        "message": self.message,
        "job_id": self.job_id,
        "attempt": self.attempt,
        "script_name": self.script_name
        }
        
class RetryableError(JobError):
    """Infrastructure failure: the job goes back to status 1 until MAX_ATTEMPTS."""
    status = "retrying"

class FatalError(JobError):
    """No Retries: status 4"""
    status = "fatal"

class JobTimeout(RetryableError):
    """Job timed out."""
    
class MemoryCapExceeded(RetryableError):
    """Job memory cap exceeded."""

class WorkerCrashed(RetryableError):
    """Internal exception when worker process dies unexpectedly."""
    
class JobNotFound(FatalError):
    """Couldn't find the job id in the queue."""

class VenvCreationException(FatalError):
    """Raised when virtual environment creation fails."""
    
class ScriptError(FatalError):
    """If the script ain't there, there's nothing python-api can do, it's related to the import systemd service."""
    
class RetriesExhausted(FatalError):
    """Retries exhausted and time budget ran out"""

    def __init__(self, message: str, job_id: str | None = None, attempt: int = 0,
                 script_name: str | None = None, last_error: JobError | None = None):
        super().__init__(message, job_id=job_id, attempt=attempt, script_name=script_name)
        self.last_error = last_error

    def to_dict(self) -> dict:
        report = super().to_dict()
        if self.last_error:
            report["last_error"] = {"type": type(self.last_error).__name__, "message": self.last_error.message}
        return report
