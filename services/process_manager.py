"""
Process Manager for safe background execution of Freqtrade CLI and long-running jobs.

Prevents duplicate bot or optimization processes across Streamlit reruns via a persistent
PID-tracked job registry, enforces concurrency limits, captures logs without blocking the UI,
and ensures safe argument passing (`shell=False`).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config_service import sanitize_log_text


UNSAFE_ARG_PATTERN = __import__("re").compile(r"[\x00\n\r]|\$\(|`")


@dataclass
class ManagedJob:
    job_id: str
    job_type: str  # 'trade_dryrun', 'backtesting', 'hyperopt', 'download_data', 'lookahead', 'webserver'
    command: list[str]
    pid: int | None
    status: str  # 'running', 'completed', 'failed', 'stopped', 'timeout'
    created_at: str
    started_at_epoch: float
    ended_at: str | None = None
    return_code: int | None = None
    log_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProcessManager:
    """
    Safe subprocess manager with duplicate process prevention, PID health verification,
    concurrency caps, and log file capture.
    """

    SINGLETON_JOB_TYPES = {"trade_dryrun", "trade_live", "backtesting", "hyperopt", "webserver"}

    def __init__(
        self,
        log_dir: str | Path,
        *,
        max_concurrent_jobs: int = 3,
        freqtrade_bin: str | None = None,
    ):
        self.log_dir = Path(log_dir).resolve()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.log_dir / "process_registry.json"
        self.max_concurrent_jobs = max(1, int(max_concurrent_jobs))
        self.freqtrade_bin = freqtrade_bin or os.getenv("FREQTRADE_BIN") or shutil.which("freqtrade")
        self._handles: dict[str, subprocess.Popen] = {}

    def resolve_freqtrade_command_prefix(self) -> tuple[bool, list[str], str]:
        """
        Determine how to invoke Freqtrade CLI in the current environment.
        Returns (available, cmd_prefix, diagnostic_message).
        """
        if self.freqtrade_bin and shutil.which(self.freqtrade_bin):
            return True, [self.freqtrade_bin], f"Menggunakan binary freqtrade: {self.freqtrade_bin}"

        found = shutil.which("freqtrade")
        if found:
            return True, [found], f"Menggunakan binary freqtrade di PATH: {found}"

        # Check if freqtrade module is runnable via current python interpreter
        try:
            res = subprocess.run(
                [sys.executable, "-c", "import freqtrade.main; print('OK')"],
                capture_output=True,
                text=True,
                timeout=4,
            )
            if res.returncode == 0 and "OK" in res.stdout:
                return True, [sys.executable, "-m", "freqtrade"], f"Menggunakan modul Python: {sys.executable} -m freqtrade"
        except Exception:
            pass

        return (
            False,
            ["freqtrade"],
            "Binary CLI 'freqtrade' belum terpasang di PATH lingkungan lokal ini (Freqtrade 2026.10-dev membutuhkan Python >=3.12 atau Docker). "
            "Gunakan Docker Compose (`docker compose up -d`) atau lingkungan virtual Python 3.12.",
        )

    @staticmethod
    def is_pid_alive(pid: int | None) -> bool:
        """Check whether an OS process with the given PID is currently alive."""
        if not pid or int(pid) <= 0:
            return False
        try:
            os.kill(int(pid), 0)
            # Check if zombie on Linux
            stat_file = Path(f"/proc/{int(pid)}/stat")
            if stat_file.is_file():
                content = stat_file.read_text(encoding="utf-8", errors="ignore")
                parts = content.split(")")
                if len(parts) > 1:
                    state_char = parts[1].strip().split()[0]
                    if state_char == "Z":
                        return False
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return False

    @staticmethod
    def validate_command_args(args: list[str]) -> None:
        """Ensure command arguments are a non-empty list of safe strings without shell injection."""
        if not isinstance(args, list) or not args:
            raise ValueError("Argumen perintah proses harus berupa list non-kosong.")
        for arg in args:
            if not isinstance(arg, str):
                raise ValueError(f"Argumen harus berupa string, ditemukan: {type(arg).__name__}")
            if UNSAFE_ARG_PATTERN.search(arg):
                raise ValueError(f"Argumen mengandung karakter shell yang tidak aman: {arg!r}")

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        if not self.registry_path.is_file():
            return {}
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_registry(self, registry: dict[str, dict[str, Any]]) -> None:
        tmp_path = self.registry_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
        tmp_path.replace(self.registry_path)

    def refresh_jobs(self, timeout_seconds: float | None = None) -> list[ManagedJob]:
        """Refresh status of all tracked jobs, reaping finished processes and enforcing timeouts."""
        reg = self._load_registry()
        changed = False
        now = time.time()

        for job_id, item in list(reg.items()):
            if item.get("status") == "running":
                pid = item.get("pid")
                proc = self._handles.get(job_id)
                rc: int | None = None
                if proc is not None:
                    rc = proc.poll()

                if rc is not None:
                    item["status"] = "completed" if rc == 0 else "failed"
                    item["return_code"] = int(rc)
                    item["ended_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    if rc != 0 and not item.get("error"):
                        item["error"] = f"Proses berhenti dengan kode keluar {rc}."
                    changed = True
                elif not self.is_pid_alive(pid):
                    # Process finished outside current Python handle
                    exit_code = self._read_exit_code_marker(item.get("log_path", ""))
                    item["return_code"] = exit_code if exit_code is not None else 0
                    item["status"] = "completed" if (exit_code in (0, None)) else "failed"
                    item["ended_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                    changed = True
                elif timeout_seconds and (now - float(item.get("started_at_epoch", now))) > timeout_seconds:
                    self.stop_job(job_id, reason="Timeout batas waktu eksekusi terlampaui")
                    reg = self._load_registry()
                    break

        if changed:
            self._save_registry(reg)

        jobs = [ManagedJob(**v) for v in reg.values()]
        jobs.sort(key=lambda j: j.started_at_epoch, reverse=True)
        return jobs

    @staticmethod
    def _read_exit_code_marker(log_path: str) -> int | None:
        if not log_path or not Path(log_path).is_file():
            return None
        try:
            tail = Path(log_path).read_text(encoding="utf-8", errors="ignore")[-500:]
            for line in reversed(tail.splitlines()):
                if line.startswith("[EXIT_CODE]="):
                    return int(line.split("=", 1)[1].strip())
        except Exception:
            pass
        return None

    def get_active_jobs(self, job_type: str | None = None) -> list[ManagedJob]:
        """Return currently running jobs, optionally filtered by job_type."""
        all_jobs = self.refresh_jobs()
        return [
            j
            for j in all_jobs
            if j.status == "running" and (job_type is None or j.job_type == job_type)
        ]

    def start_job(
        self,
        job_type: str,
        command: list[str],
        *,
        metadata: dict[str, Any] | None = None,
        cwd: str | Path | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> ManagedJob:
        """
        Start a background job safely with duplicate prevention and concurrency control.
        """
        self.validate_command_args(command)
        active = self.get_active_jobs()

        # 1. Duplicate process prevention for singleton job types
        if job_type in self.SINGLETON_JOB_TYPES:
            existing = [j for j in active if j.job_type == job_type]
            if existing:
                raise RuntimeError(
                    f"Pekerjaan '{job_type}' sudah berjalan dengan PID {existing[0].pid} "
                    f"(Job ID: {existing[0].job_id}). Tidak meluncurkan proses duplikat."
                )

        # 2. Concurrency limit enforcement
        if len(active) >= self.max_concurrent_jobs:
            raise RuntimeError(
                f"Batas maksimum pekerjaan paralel ({self.max_concurrent_jobs}) tercapai. "
                "Tunggu pekerjaan aktif selesai atau hentikan terlebih dahulu."
            )

        job_id = f"{job_type}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        log_file = self.log_dir / f"{job_id}.log"
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        env = os.environ.copy()
        if env_overrides:
            env.update({str(k): str(v) for k, v in env_overrides.items()})

        try:
            log_fh = open(log_file, "w", encoding="utf-8")
            log_fh.write(f"=== JOB STARTED: {job_id} ({job_type}) AT {now_iso} ===\n")
            log_fh.write(f"COMMAND: {sanitize_log_text(' '.join(command))}\n\n")
            log_fh.flush()

            proc = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=True,
            )
            log_fh.close()
            self._handles[job_id] = proc

            job = ManagedJob(
                job_id=job_id,
                job_type=job_type,
                command=[sanitize_log_text(c) for c in command],
                pid=proc.pid,
                status="running",
                created_at=now_iso,
                started_at_epoch=time.time(),
                log_path=str(log_file),
                metadata=metadata or {},
            )
        except FileNotFoundError as exc:
            job = ManagedJob(
                job_id=job_id,
                job_type=job_type,
                command=[sanitize_log_text(c) for c in command],
                pid=None,
                status="failed",
                created_at=now_iso,
                started_at_epoch=time.time(),
                ended_at=now_iso,
                return_code=127,
                log_path=str(log_file),
                metadata=metadata or {},
                error=f"Executable tidak ditemukan: {exc}",
            )
        except Exception as exc:
            job = ManagedJob(
                job_id=job_id,
                job_type=job_type,
                command=[sanitize_log_text(c) for c in command],
                pid=None,
                status="failed",
                created_at=now_iso,
                started_at_epoch=time.time(),
                ended_at=now_iso,
                return_code=1,
                log_path=str(log_file),
                metadata=metadata or {},
                error=sanitize_log_text(str(exc)),
            )

        reg = self._load_registry()
        reg[job_id] = job.to_dict()
        self._save_registry(reg)
        return job

    def stop_job(self, job_id: str, reason: str = "Dihentikan oleh pengguna") -> ManagedJob | None:
        """Gracefully stop a running job by sending SIGTERM, then SIGKILL if needed."""
        reg = self._load_registry()
        item = reg.get(job_id)
        if not item:
            return None

        pid = item.get("pid")
        proc = self._handles.get(job_id)

        if pid and self.is_pid_alive(pid):
            try:
                os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
            except Exception:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except Exception:
                    pass

            deadline = time.time() + 3.0
            while time.time() < deadline and self.is_pid_alive(pid):
                if proc is not None and proc.poll() is not None:
                    break
                time.sleep(0.1)

            if self.is_pid_alive(pid):
                try:
                    os.killpg(os.getpgid(int(pid)), signal.SIGKILL)
                except Exception:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except Exception:
                        pass

        if proc is not None:
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass

        item["status"] = "stopped"
        item["ended_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        item["error"] = reason
        reg[job_id] = item
        self._save_registry(reg)
        return ManagedJob(**item)

    def get_job(self, job_id: str) -> ManagedJob | None:
        self.refresh_jobs()
        reg = self._load_registry()
        item = reg.get(job_id)
        return ManagedJob(**item) if item else None

    def read_job_log(self, job_id: str, max_lines: int = 200) -> str:
        """Read and sanitize the last `max_lines` lines from a job's log file."""
        job = self.get_job(job_id)
        if not job or not job.log_path:
            return "Log belum tersedia untuk pekerjaan ini."
        path = Path(job.log_path)
        if not path.is_file():
            return "File log belum terbentuk."
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-max_lines:])
            return sanitize_log_text(tail)
        except Exception as exc:
            return f"Gagal membaca log: {sanitize_log_text(str(exc))}"
