"""Reproducible offline verification of the pinned official SNOWPACK example."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .snowpack_output import parse_snowpack_smet


SNOWPACK_VERSION = "3.7.0"
SNOWPACK_COMMIT = "349b857af07ddb090b3e7b36fb6a45ec87ec2338"
SNOWPACK_TAG = "Snowpack-3.7.0"
EXAMPLE_END = "1996-06-17T00:00"
EXAMPLE_FILES = (
    "doc/examples/cfgfiles/io_res1exp.ini",
    "doc/examples/input/MST96.smet",
    "doc/examples/input/MST96.sno",
)
OUTPUT_FILES = (
    "output/MST96_res.ini",
    "output/MST96_res.pro",
    "output/MST96_res.smet",
)


class OfficialExampleError(RuntimeError):
    """Raised when pinned provenance or the official smoke contract fails."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_record(path: Path) -> dict[str, Any]:
    content = path.read_bytes()
    return {"bytes": len(content), "sha256": _sha256(content)}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8") + b"\n"


def _git(source_root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(source_root), *arguments],
        capture_output=True,
        check=False,
        shell=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise OfficialExampleError(
            completed.stderr.decode("utf-8", errors="replace")[:1000]
        )
    return completed.stdout


def load_official_example_verification(path: str | Path) -> dict[str, Any]:
    """Strictly validate a complete immutable example-verification directory."""

    root = Path(path).resolve()
    try:
        report = json.loads((root / "report.json").read_text(encoding="utf-8"))
        checksums = json.loads((root / "checksums.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfficialExampleError(f"Invalid official-example verification: {exc}") from exc
    verification_id = report.get("verification_id")
    if not isinstance(verification_id, str) or root.name != verification_id:
        raise OfficialExampleError("Verification directory and report identities differ.")
    draft = dict(report)
    draft.pop("verification_id", None)
    expected_id = f"verification-{_sha256(_canonical(draft))}"
    if verification_id != expected_id:
        raise OfficialExampleError("Verification identity does not match report content.")
    actual_files = {
        str(item.relative_to(root)).replace("\\", "/"): _file_record(item)
        for item in sorted(root.rglob("*"))
        if item.is_file() and item.name != "checksums.json"
    }
    expected_checksums = {
        "schema": "snowpack-official-example-storage-v1",
        "verification_id": verification_id,
        "files": actual_files,
    }
    if checksums != expected_checksums:
        raise OfficialExampleError("Verification checksum manifest does not match stored bytes.")
    if report.get("source", {}).get("commit") != SNOWPACK_COMMIT:
        raise OfficialExampleError("Verification does not bind the pinned source commit.")
    if report.get("result") != "PASS_OFFICIAL_SMOKE_ONLY":
        raise OfficialExampleError("Verification does not contain a passing official smoke result.")
    return report


def verify_official_example(
    executable: str | Path,
    source_root: str | Path,
    runtime_root: str | Path,
    *,
    timeout_seconds: float = 120.0,
) -> Path:
    """Run unchanged example inputs and atomically preserve all evidence."""

    exe = Path(executable).resolve()
    source = Path(source_root).resolve()
    runtime = Path(runtime_root).resolve()
    if not exe.is_file() or not source.is_dir() or timeout_seconds <= 0:
        raise OfficialExampleError("Executable, source tree, or timeout is invalid.")
    commit = _git(source, "rev-parse", "HEAD").decode("ascii", errors="strict").strip()
    if commit != SNOWPACK_COMMIT:
        raise OfficialExampleError(f"Source commit is {commit!r}, not pinned {SNOWPACK_COMMIT}.")
    if _git(source, "status", "--porcelain", "--untracked-files=no").strip():
        raise OfficialExampleError("Pinned SNOWPACK source has tracked working-tree changes.")
    source_contents: dict[str, bytes] = {}
    for relative in EXAMPLE_FILES:
        committed = _git(source, "show", f"HEAD:{relative}")
        source_contents[relative] = committed

    version = subprocess.run(
        [str(exe), "--version"], capture_output=True, check=False, shell=False, timeout=30
    )
    version_bytes = version.stdout + version.stderr
    version_output = version_bytes.decode("utf-8", errors="strict").strip()
    if version.returncode != 0 or f"Snowpack version {SNOWPACK_VERSION}" not in version_output:
        raise OfficialExampleError("Executable did not report the pinned SNOWPACK version.")

    reports_root = runtime / "reports" / "snowpack"
    reports_root.mkdir(parents=True, exist_ok=True)
    staging = reports_root / f".official-example-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for relative, content in source_contents.items():
            destination_relative = Path(relative).relative_to("doc/examples")
            destination = staging / destination_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(content)
        (staging / "output").mkdir()
        arguments = ("-c", "cfgfiles/io_res1exp.ini", "-e", EXAMPLE_END)
        completed = subprocess.run(
            [str(exe), *arguments],
            cwd=staging,
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout_seconds,
        )
        combined = completed.stdout + b"\n" + completed.stderr
        smoke_pattern_absent = re.search(b"error|differ", combined, re.IGNORECASE) is None
        if completed.returncode != 0 or not smoke_pattern_absent:
            raise OfficialExampleError(
                "Official res1exp smoke expectation failed: "
                f"exit={completed.returncode}, forbidden_pattern_absent={smoke_pattern_absent}."
            )
        for relative in OUTPUT_FILES:
            path = staging / relative
            if not path.is_file() or path.stat().st_size == 0:
                raise OfficialExampleError(f"Official example output is missing: {relative}")
        stdout_path = staging / "stdout.bin"
        stderr_path = staging / "stderr.bin"
        stdout_path.write_bytes(completed.stdout)
        stderr_path.write_bytes(completed.stderr)

        parsed = parse_snowpack_smet((staging / "output/MST96_res.smet").read_bytes())
        state_bounds: dict[str, dict[str, float | int | None]] = {}
        for field in ("HS_mod", "SWE", "TSS_mod"):
            present = tuple(value for value in parsed.values[field] if value is not None)
            if field in {"HS_mod", "SWE"} and any(value < 0 for value in present):
                raise OfficialExampleError(f"Official example produced negative {field} values.")
            if field == "TSS_mod" and any(value <= 0 for value in present):
                raise OfficialExampleError("Official example produced non-kelvin TSS_mod values.")
            state_bounds[field] = {
                "present": len(present),
                "missing": len(parsed.timestamps_utc) - len(present),
                "minimum": min(present) if present else None,
                "maximum": max(present) if present else None,
            }

        report_draft = {
            "schema": "snowpack-official-example-verification-v1",
            "source": {
                "official_repository": "https://code.wsl.ch/snow-models/snowpack",
                "tag": SNOWPACK_TAG,
                "commit": commit,
                "licence": "LGPL-3.0-or-later",
                "files": {
                    str(Path(relative).relative_to("doc/examples")): {
                        "bytes": len(content), "sha256": _sha256(content)
                    }
                    for relative, content in sorted(source_contents.items())
                },
            },
            "executable": {
                "path": str(exe),
                **_file_record(exe),
                "version_output": version_output,
                "version_output_sha256": _sha256(version_bytes),
            },
            "process": {
                "command": [str(exe), *arguments],
                "timeout_seconds": timeout_seconds,
                "exit_code": completed.returncode,
                "official_smoke_forbidden_pattern": "error|differ",
                "official_smoke_forbidden_pattern_absent": smoke_pattern_absent,
                "stdout": _file_record(stdout_path),
                "stderr": _file_record(stderr_path),
            },
            "outputs": {
                relative: _file_record(staging / relative) for relative in OUTPUT_FILES
            },
            "parsed_smet": {
                "parser_version": "snowpack-3.7.0-smet-output-v1",
                "raw_sha256": parsed.raw_sha256,
                "normalized_sha256": parsed.normalized_sha256,
                "records": len(parsed.timestamps_utc),
                "fields": len(parsed.fields),
                "start_utc": parsed.timestamps_utc[0].isoformat().replace("+00:00", "Z"),
                "end_utc": parsed.timestamps_utc[-1].isoformat().replace("+00:00", "Z"),
                "state_unit_and_bounds_sanity": state_bounds,
            },
            "result": "PASS_OFFICIAL_SMOKE_ONLY",
            "limitations": [
                "This verifies the official bundled example and executable, not a Mount Hosmer configuration.",
                "The example has Swiss site inputs and must not supply Hosmer soil, canopy, roughness, or initialization values.",
                "The official smoke test does not establish field accuracy or a mass/energy closure benchmark.",
            ],
        }
        verification_digest = _sha256(_canonical(report_draft))
        verification_id = f"verification-{verification_digest}"
        report = {**report_draft, "verification_id": verification_id}
        report_path = staging / "report.json"
        report_path.write_bytes(_canonical(report))

        files = {
            str(path.relative_to(staging)).replace("\\", "/"): _file_record(path)
            for path in sorted(staging.rglob("*"))
            if path.is_file()
        }
        checksums = {
            "schema": "snowpack-official-example-storage-v1",
            "verification_id": verification_id,
            "files": files,
        }
        (staging / "checksums.json").write_bytes(_canonical(checksums))
        for path in staging.rglob("*"):
            if path.is_file():
                with path.open("r+b") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
        target = reports_root / verification_id
        if target.exists():
            load_official_example_verification(target)
            shutil.rmtree(staging)
            return target
        staging.rename(target)
        return target
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
