"""Bounded external-process execution for offline snow-model experiments."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class SnowProcessError(RuntimeError):
    """Raised for version, timeout, process, path, or output-contract failures."""


@dataclass(frozen=True)
class ExternalProcessResult:
    executable_sha256: str
    version_output: str
    command_argv: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes
    output: bytes


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def execute_snow_process(
    executable: str | Path,
    arguments: Sequence[str],
    *,
    input_files: Mapping[str, bytes],
    output_relative_path: str,
    timeout_seconds: float,
    version_arguments: Sequence[str] = ("--version",),
    maximum_output_bytes: int = 500 * 1024 * 1024,
) -> ExternalProcessResult:
    """Execute in a disposable directory and return immutable captured bytes."""

    exe = Path(executable).resolve()
    if not exe.is_file() or timeout_seconds <= 0:
        raise SnowProcessError("Executable is missing or timeout is invalid.")
    output_rel = Path(output_relative_path)
    if output_rel.is_absolute() or ".." in output_rel.parts or not output_rel.parts:
        raise SnowProcessError("Output path must be a safe relative path.")
    for relative in input_files:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise SnowProcessError("Input path must be a safe relative path.")
    try:
        version = subprocess.run(
            [str(exe), *version_arguments], capture_output=True, check=False,
            timeout=min(timeout_seconds, 30.0), shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SnowProcessError(f"Could not identify external executable: {exc}") from exc
    version_bytes = version.stdout + version.stderr
    version_output = version_bytes.decode("utf-8", errors="strict").strip()
    if version.returncode != 0 or not version_output:
        raise SnowProcessError("External executable version command failed or was empty.")

    with tempfile.TemporaryDirectory(prefix="mount-hosmer-snow-run-") as raw:
        work = Path(raw).resolve()
        for relative, content in sorted(input_files.items()):
            target = (work / relative).resolve()
            if not target.is_relative_to(work):
                raise SnowProcessError("Input path escaped the disposable run directory.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        argv = (str(exe), *tuple(str(item) for item in arguments))
        try:
            completed = subprocess.run(
                argv, cwd=work, capture_output=True, check=False,
                timeout=timeout_seconds, shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SnowProcessError(f"External snow process exceeded {timeout_seconds} seconds.") from exc
        except OSError as exc:
            raise SnowProcessError(f"External snow process could not start: {exc}") from exc
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace")
            raise SnowProcessError(
                f"External snow process failed with exit {completed.returncode}: {stderr[:1000]}"
            )
        output_path = (work / output_rel).resolve()
        if not output_path.is_relative_to(work) or not output_path.is_file():
            raise SnowProcessError("External snow process did not create the required output.")
        size = output_path.stat().st_size
        if size <= 0 or size > maximum_output_bytes:
            raise SnowProcessError("External snow output is empty or exceeds the configured bound.")
        output = output_path.read_bytes()
        return ExternalProcessResult(
            executable_sha256=_file_sha256(exe),
            version_output=version_output,
            command_argv=argv,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            output=output,
        )
