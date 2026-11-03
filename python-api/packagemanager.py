#!/usr/bin/env python3

import hashlib
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from exceptions import JobError, JobTimeout, ScriptError, VenvCreationException
from metrics import VENV_CACHE_HITS, VENV_CORRUPTIONS, VENV_CREATIONS

if TYPE_CHECKING:
    from workerv2 import Job    # annotations only: workerv2 imports this module

logger = logging.getLogger(__name__)

##############################################################
# Virtual environment handling area
##############################################################

class PackageManager:
    """
    Manages isolated virtual environments per script with dependency caching.
    Scripts with the same extra requirements share one venv (named by a hash of the requirements).

    Every failure goes up the chain as a JobError carrying the job's id, attempt and script name:
    - VenvCreationException: venv or pip failed, or a corrupted venv could not be purged
    - JobTimeout: the job's request budget ran out while waiting for or building the venv
    - ScriptError: the script file cannot be read
    There is no fallback to system Python: a script that needs packages never runs without them.

    A venv counts as ready only once READY_MARKER exists (written last), so a half-built venv,
    e.g. left by a container restart, is rebuilt instead of reused.
    """
    READY_MARKER = ".requirements"  # lists what was installed; its mtime is the venv's last use

    def __init__(self, scripts_dir: Path, venv_base: Path):
        self.scripts_dir = Path(scripts_dir)
        self.venv_base = Path(venv_base)
        self.venv_base.mkdir(parents=True, exist_ok=True)
        self.base_packages = self._get_base_packages()
        self._corrupted_venvs: set[Path] = set()
        self._venv_locks: dict[Path, threading.Lock] = {}  # one per venv: a single build at a time

    @staticmethod
    def _fail(error: type[JobError], message: str, job: "Job") -> JobError:
        """Build the error with the job's id, attempt and script name, ready to raise."""
        return error(message, job_id=job.job_id, attempt=job.attempts, script_name=job.script)

    @staticmethod
    def _package_name(requirement: str) -> str:
        """'Typing_Extensions[x]>=4.0' -> 'typing-extensions' (PEP 503 name, so pip list and # requires: compare equal)"""
        match = re.match(r"[A-Za-z0-9._-]+", requirement.strip())
        name = match.group(0) if match else requirement.strip()
        return re.sub(r"[-_.]+", "-", name).lower()

    def _get_base_packages(self) -> set[str]:
        """Get list of pre-installed packages in main environment. Runs at startup: a failure stops startup."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "list", "--format=freeze"],
                capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise VenvCreationException("Failed to get base packages: pip list timed out after 60s") from None
        if result.returncode != 0:
            raise VenvCreationException(f"Failed to get base packages: {result.stderr[-2000:]}")
        return {self._package_name(line) for line in result.stdout.splitlines() if "==" in line}

    def _get_script_requirements(self, job: "Job") -> list[str]:
        """Extract requirements from script header comments"""
        script_path = self.scripts_dir / job.script
        requirements = []

        try:
            with open(script_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("# requires:"):
                        # Format: # requires: requests==2.31.0, pandas
                        reqs = line.replace("# requires:", "").strip()
                        requirements.extend(r.strip() for r in reqs.split(","))
                    elif not line.startswith("#"):
                        break  # Stop at first non-comment
        except OSError as e:
            raise self._fail(ScriptError, f"Cannot read script {script_path}: {e}", job) from None

        return [r for r in requirements if r]

    def _get_venv_path(self, requirements: list[str]) -> Path | None:
        """
        Generate venv path based on requirements hash.
        This allows venv to be reused across scripts with identical dependencies.
        """
        if not requirements:
            return None  # Signal to use system Python

        # Normalize: lowercase, strip whitespace, sort
        normalized = sorted(r.strip().lower() for r in requirements)
        req_str = "|".join(normalized)

        req_hash = hashlib.sha256(req_str.encode()).hexdigest()[:16]
        return self.venv_base / req_hash  # No script name - pure hash made from reqs.

    def _venv_lock(self, venv_path: Path) -> threading.Lock:
        return self._venv_locks.setdefault(venv_path, threading.Lock())  # setdefault is atomic under the GIL

    def mark_venv_corrupted(self, venv_path: Path | None):
        """Mark venv for purging. The runner calls it when a script fails on a missing module;
        the next prepare_environment for this venv deletes and rebuilds it."""
        if venv_path:
            self._corrupted_venvs.add(venv_path)
            logger.warning(f"Marked venv as corrupted: {venv_path}")
            VENV_CORRUPTIONS.inc()

    def purge_corrupted_venv(self, venv_path: Path, job: "Job"):
        """Delete a corrupted venv. Call with the venv's lock held."""
        try:
            if venv_path.exists():
                shutil.rmtree(venv_path)
        except OSError as e:
            raise self._fail(VenvCreationException, f"Cannot purge corrupted venv {venv_path}: {e}", job) from None
        self._corrupted_venvs.discard(venv_path)
        logger.info(f"job {job.job_id}: purged corrupted venv {venv_path}")

    def prepare_environment(self, job: "Job") -> tuple[str, Path, Path | None]:
        """
        Returns (python_exe, scripts_dir, venv_path_or_none) for the job's script.
        System Python if the script needs nothing beyond the base packages; otherwise the venv for
        its requirements: reused if ready, (re)built if missing, half-built or marked corrupted.
        Blocking (pip can take minutes): the runner calls it with asyncio.to_thread.
        Raises VenvCreationException, JobTimeout or ScriptError (see class docstring).
        """
        requirements = self._get_script_requirements(job)
        extra_requirements = [r for r in requirements if self._package_name(r) not in self.base_packages]

        venv_path = self._get_venv_path(extra_requirements)
        if venv_path is None:
            return (sys.executable, self.scripts_dir, None)

        # Jobs with the same requirements wait here while one of them builds the venv
        lock = self._venv_lock(venv_path)
        if not lock.acquire(timeout=job.time_left()):
            raise self._fail(JobTimeout, f"Budget spent waiting for venv {venv_path.name} to be built", job)
        try:
            if venv_path in self._corrupted_venvs:
                self.purge_corrupted_venv(venv_path, job)

            marker = venv_path / self.READY_MARKER
            if marker.exists():
                # REUSE
                marker.touch()
                VENV_CACHE_HITS.inc()
            else:
                # CREATE
                self._create_venv(venv_path, extra_requirements, job)
        finally:
            lock.release()

        return (str(venv_path / "bin" / "python"), self.scripts_dir, venv_path)

    def _create_venv(self, venv_path: Path, requirements: list[str], job: "Job"):
        """Create virtual environment and install packages, within the job's time budget.
        On any failure the half-built venv is deleted, so the next request starts clean."""
        if venv_path.exists():
            # Half-built leftover (no READY_MARKER), e.g. after a container restart
            self.purge_corrupted_venv(venv_path, job)

        python_exe = venv_path / "bin" / "python"
        steps = [
            ("create venv", [sys.executable, "-m", "venv", "--system-site-packages", str(venv_path)]),
            (f"install {', '.join(requirements)}",
             [str(python_exe), "-m", "pip", "install", "--disable-pip-version-check", *requirements]),
        ]
        try:
            for step, command in steps:
                self._run_build_step(step, command, job)
        except JobError:
            shutil.rmtree(venv_path, ignore_errors=True)
            raise

        # Persist venv metadata; from here on the venv counts as ready
        (venv_path / self.READY_MARKER).write_text("\n".join(requirements))
        logger.info(f"job {job.job_id}: created venv {venv_path} for {job.script}")
        VENV_CREATIONS.inc()

    def _run_build_step(self, step: str, command: list[str], job: "Job"):
        """Run one venv build command with the rest of the job's budget as timeout."""
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=job.time_left())
        except subprocess.TimeoutExpired:
            raise self._fail(JobTimeout, f"Budget spent during venv build step: {step}", job) from None
        except OSError as e:
            raise self._fail(VenvCreationException, f"Venv build step '{step}' could not start: {e}", job) from None
        if result.returncode != 0:
            raise self._fail(VenvCreationException, f"Venv build step '{step}' failed: {result.stderr[-2000:]}", job)

    def _cleanup_old_venvs(self, max_age_days: int = 7):
        """Remove venvs unused for max_age_days, and half-built ones (no READY_MARKER).
        Maintenance, not a job: failures are logged, not raised. Venvs being built are skipped."""
        cutoff = time.time() - max_age_days * 86400
        for venv_dir in self.venv_base.iterdir():
            if not venv_dir.is_dir():
                continue
            marker = venv_dir / self.READY_MARKER
            if marker.exists() and marker.stat().st_mtime > cutoff:
                continue
            lock = self._venv_lock(venv_dir)
            if not lock.acquire(blocking=False):
                continue
            try:
                shutil.rmtree(venv_dir)
                self._corrupted_venvs.discard(venv_dir)
            except OSError as e:
                logger.error(f"Failed to cleanup {venv_dir}: {e}")
            finally:
                lock.release()
