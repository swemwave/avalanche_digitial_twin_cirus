"""Bounded subprocess utilities shared by offline runout adapters."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from avycore.engines import AvailabilityStatus, EngineAvailability


class ExternalModelProcessError(RuntimeError):
    """A visible external-model launch, timeout, exit, or output failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProcessCapture:
    executable_sha256: str
    argv: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(path: str | Path, expected_sha256: str, expected_size: int) -> Path:
    requested = Path(path).resolve()
    if not requested.is_file():
        raise ExternalModelProcessError("missing_input_artifact", f"Input artifact is missing: {requested}")
    actual_size = requested.stat().st_size
    if actual_size != expected_size:
        raise ExternalModelProcessError(
            "input_size_mismatch",
            f"Input artifact size mismatch for {requested}: expected {expected_size}, got {actual_size}.",
        )
    actual_sha256 = file_sha256(requested)
    if actual_sha256 != expected_sha256:
        raise ExternalModelProcessError(
            "input_hash_mismatch",
            f"Input artifact SHA-256 mismatch for {requested}.",
        )
    return requested


def probe_python_distribution(
    python_executable: str | Path,
    *,
    engine_id: str,
    distribution: str,
    import_name: str,
    timeout_seconds: float = 20.0,
) -> EngineAvailability:
    python = Path(python_executable).resolve()
    if not python.is_file():
        return EngineAvailability(
            engine_id=engine_id,
            status=AvailabilityStatus.UNAVAILABLE,
            reason=f"Configured Python executable does not exist: {python}",
        )
    probe = (
        "import importlib,importlib.metadata,json;"
        f"importlib.import_module({import_name!r});"
        f"print(json.dumps({{'version':importlib.metadata.version({distribution!r})}}))"
    )
    try:
        completed = subprocess.run(
            [str(python), "-I", "-c", probe],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            shell=False,
            env=_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return EngineAvailability(
            engine_id=engine_id,
            status=AvailabilityStatus.UNAVAILABLE,
            reason=f"Version probe exceeded {timeout_seconds:g} seconds.",
        )
    except OSError as exc:
        return EngineAvailability(
            engine_id=engine_id,
            status=AvailabilityStatus.UNAVAILABLE,
            reason=f"Version probe could not start: {exc}",
        )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:1000]
        return EngineAvailability(
            engine_id=engine_id,
            status=AvailabilityStatus.UNAVAILABLE,
            reason=f"Could not import {import_name!r} in the isolated environment: {detail}",
        )
    try:
        version = json.loads(completed.stdout.decode("utf-8"))["version"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return EngineAvailability(
            engine_id=engine_id,
            status=AvailabilityStatus.MISCONFIGURED,
            reason=f"Version probe returned an invalid response: {exc}",
        )
    return EngineAvailability(
        engine_id=engine_id,
        status=AvailabilityStatus.AVAILABLE,
        reason=f"Imported {import_name!r} from the configured isolated Python environment.",
        detected_version=str(version),
        executable_sha256=file_sha256(python),
    )


def run_isolated_worker(
    python_executable: str | Path,
    worker_script: str | Path,
    arguments: Sequence[str],
    *,
    cwd: str | Path,
    timeout_seconds: float,
    maximum_capture_bytes: int = 4 * 1024 * 1024,
) -> ProcessCapture:
    python = Path(python_executable).resolve()
    worker = Path(worker_script).resolve()
    work = Path(cwd).resolve()
    if not python.is_file():
        raise ExternalModelProcessError("missing_executable", f"Python executable is missing: {python}")
    if not worker.is_file():
        raise ExternalModelProcessError("missing_worker", f"Worker script is missing: {worker}")
    if not work.is_dir() or timeout_seconds <= 0:
        raise ExternalModelProcessError("invalid_work_directory", "Work directory or timeout is invalid.")
    argv = (str(python), "-I", str(worker), *(str(item) for item in arguments))
    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            completed = subprocess.run(
                argv,
                cwd=work,
                stdout=stdout_file,
                stderr=stderr_file,
                check=False,
                timeout=timeout_seconds,
                shell=False,
                env=_subprocess_env(),
            )
            stdout_size = stdout_file.tell()
            stderr_size = stderr_file.tell()
            if stdout_size > maximum_capture_bytes or stderr_size > maximum_capture_bytes:
                raise ExternalModelProcessError(
                    "capture_too_large",
                    "External model stdout or stderr exceeded the configured bound.",
                )
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
    except subprocess.TimeoutExpired as exc:
        raise ExternalModelProcessError(
            "timeout", f"External model exceeded {timeout_seconds:g} seconds."
        ) from exc
    except OSError as exc:
        raise ExternalModelProcessError(
            "launch_failed", f"External model could not start: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[:2000]
        raise ExternalModelProcessError(
            "nonzero_exit",
            f"External model failed with exit {completed.returncode}: {detail}",
        )
    return ProcessCapture(
        executable_sha256=file_sha256(python),
        argv=argv,
        exit_code=completed.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONNOUSERSITE"] = "1"
    env["MPLBACKEND"] = "Agg"
    return env


__all__ = [
    "ExternalModelProcessError",
    "ProcessCapture",
    "file_sha256",
    "probe_python_distribution",
    "run_isolated_worker",
    "verify_artifact",
]
