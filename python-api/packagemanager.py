#!/usr/bin/env python3

import sys
import venv
import hashlib
import shutil
from pathlib import Path
import subprocess
from typing import Optional, Tuple
import time
import logging
from exceptions import VenvCreationException

logger = logging.getLogger(__name__)

##############################################################
# Virtual environment handling area
##############################################################

class PackageManager:
    """
    Manages isolated virtual environments per script with dependency caching.
    Each script gets its own venv if it has unique requirements.
    """

    VENV_BASE = Path("/app/venvs")
    SCRIPTS_DIR = Path("/app/scripts")

    def __init__(self):
        self.VENV_BASE.mkdir(parents=True, exist_ok=True)
        self.base_packages = self._get_base_packages()
        self._corrupted_venvs: set[Path] = set()

    def _get_base_packages(self) -> set:
        """Get list of pre-installed packages in main environment"""
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=freeze"],
            capture_output=True, text=True
        )
        return set(line.split("==")[0].lower() for line in result.stdout.splitlines() if "==" in line)

    def _get_script_requirements(self, script_name: str) -> list[str]:
        """Extract requirements from script header comments"""
        script_path = self.SCRIPTS_DIR / script_name
        requirements = []
        
        with open(script_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("# requires:"):
                    # Format: # requires: requests==2.31.0, pandas
                    reqs = line.replace("# requires:", "").strip()
                    requirements.extend(r.strip() for r in reqs.split(","))
                elif not line.startswith("#"):
                    break  # Stop at first non-comment
        
        return [r for r in requirements if r]

    def _get_venv_path(self, requirements: list[str]) -> Optional[Path]:
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
        return self.VENV_BASE / req_hash  # No script name - pure hash made from reqs.
    
    def mark_venv_corrupted(self, venv_path: Optional[Path]):
        """Mark venv for purging"""
        if venv_path:
            self._corrupted_venvs.add(venv_path)
            logger.warning(f"Marked venv as corrupted: {venv_path}")

    def purge_corrupted_venv(self, venv_path: Path):
        """Remove and recreate venv"""
        if venv_path in self._corrupted_venvs:
            try:
                if venv_path.exists():
                    shutil.rmtree(venv_path)
                    logger.info(f"Purged corrupted venv: {venv_path}")
                self._corrupted_venvs.discard(venv_path)
                return True
            except Exception as e:
                logger.error(f"Failed to purge venv {venv_path}: {e}")
                return False
        return False

    def prepare_environment(self, script_name: str) -> Tuple[str, Path, Optional[Path]]:
        """
        Returns (python_exe, scripts_dir, venv_path_or_none)
        Reuses existing venvs if requirements match.
        Falls back to system Python on failure.
        """
        requirements = self._get_script_requirements(script_name)
        
        # Filter out base packages
        extra_requirements = [
            r for r in requirements 
            if r.split("==")[0].split(">")[0].split("<")[0].strip().lower() 
            not in self.base_packages
        ]
        
        venv_path = self._get_venv_path(extra_requirements)
        
        # Check if venv was marked corrupted and purge
        if venv_path and venv_path in self._corrupted_venvs:
            self.purge_corrupted_venv(venv_path)
        
        if venv_path is None:
            return (sys.executable, self.SCRIPTS_DIR, None)
        
        # REUSE: Check if venv already exists
        if venv_path.exists():
            return (str(venv_path / "bin" / "python"), self.SCRIPTS_DIR, venv_path)
        
        # CREATE: Build new venv with error handling
        try:
            self._create_venv(venv_path, extra_requirements)
            return (str(venv_path / "bin" / "python"), self.SCRIPTS_DIR, venv_path)
        except Exception as e:
            logger.error(f"Venv creation failed for {script_name}: {e}")
            # Fallback to system Python
            return (sys.executable, self.SCRIPTS_DIR, None)

    def _create_venv(self, venv_path: Path, requirements: list[str]):
        """Create virtual environment and install packages"""
        
        # Create venv
        try:
            venv.create(venv_path, with_pip=True, system_site_packages=True)
        except Exception as e:
            raise VenvCreationException(f"Failed to create venv: {e}")
        
        # Install requirements
        pip = venv_path / "bin" / "pip"
        
        for req in requirements:
            try:
                subprocess.run(
                    [str(pip), "install", req],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=300
                )
            except subprocess.CalledProcessError as e:
                # MARK: MODIFIED - Mark venv corrupted, raise
                self.mark_venv_corrupted(venv_path)
                raise VenvCreationException(f"Failed to install {req}: {e.stderr}")
            except subprocess.TimeoutExpired:
                self.mark_venv_corrupted(venv_path)
                raise VenvCreationException(f"Timeout installing {req}")
            
        # Persist venv metadata
        (venv_path / ".requirements").write_text("\n".join(requirements))

    def _cleanup_old_venvs(self, max_age_days: int = 7):
        """Remove venvs older than max_age_days."""
        now = time.time()
        for venv_dir in self.VENV_BASE.iterdir():
            if not venv_dir.is_dir():
                continue
            mtime = venv_dir.stat().st_mtime
            if (now - mtime) > (max_age_days * 86400):
                try:
                    shutil.rmtree(venv_dir)
                    self._corrupted_venvs.discard(venv_dir)
                except Exception as e:
                    logger.error(f"Failed to cleanup {venv_dir}: {e}")