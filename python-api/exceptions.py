#!/usr/bin/env python3
"""
Custom exceptions for worker management and job retry logic.
"""


class JobRetryingException(Exception):
    """
    Raised when a job must be retried due to worker death.
    HTTP response will return 202 with this message.
    """
    
    def __init__(self, message: str, job_id: str, attempt: int, script_name: str | None):
        self.message = message
        self.job_id = job_id
        self.attempt = attempt
        self.script_name = script_name
        super().__init__(self.message)
    
    def to_dict(self):
        return {
            "status": "retrying",
            "message": self.message,
            "job_id": self.job_id,
            "attempt": self.attempt,
            "script_name": self.script_name
        }


class WorkerDiedException(Exception):
    """Internal exception when worker process dies unexpectedly."""
    pass


class VenvCreationException(Exception):
    """Raised when virtual environment creation fails."""
    pass