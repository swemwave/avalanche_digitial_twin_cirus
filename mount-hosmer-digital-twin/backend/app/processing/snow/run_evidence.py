"""Derived, immutable evidence for an isolated SNOWPACK external-process run."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from .execution import ExternalProcessResult
from .smet import SMET_ADAPTER_VERSION, SMET_FIELDS
from .snowpack_output import PARSER_VERSION, ParsedSnowpackSmet, require_exact_cadence


RUN_EVIDENCE_SCHEMA = "mount-hosmer-snowpack-run-evidence-v1"
MODEL_INPUT_REPLAY_SCHEMA = "mount-hosmer-snowpack-model-input-replay-v1"
REQUIRED_INPUT_ROLES = frozenset(
    {"configuration", "forcing", "initial_state", "site_parameters"}
)


class SnowpackRunEvidenceError(ValueError):
    """Raised when the executable or complete model-input closure is ambiguous."""


@dataclass(frozen=True)
class EvidenceFile:
    relative_path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class SnowpackRunEvidence:
    schema_version: str
    engine: str
    executable_sha256: str
    executable_version_output: str
    binary_files: tuple[EvidenceFile, ...]
    binary_inventory_sha256: str
    input_files: tuple[EvidenceFile, ...]
    input_roles: Mapping[str, tuple[str, ...]]
    input_role_inventory_sha256: Mapping[str, str]
    input_inventory_sha256: str
    forcing_adapter_version: str
    output_parser_version: str
    relative_command_argv: tuple[str, ...]
    timeout_seconds: float
    exit_code: int
    stdout_sha256: str
    stdout_bytes: int
    stderr_sha256: str
    stderr_bytes: int
    raw_output_sha256: str
    raw_output_bytes: int
    normalized_output_sha256: str
    model_input_replay_sha256: str
    run_evidence_id: str


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(value: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or path.as_posix() in {"", "."}:
        raise SnowpackRunEvidenceError(f"Unsafe or empty relative input path: {value!r}.")
    return path.as_posix()


def derive_binary_inventory(executable: str | Path) -> tuple[tuple[EvidenceFile, ...], str]:
    """Hash the executable plus every adjacent project-local runtime DLL."""

    exe = Path(executable).resolve()
    if not exe.is_file():
        raise SnowpackRunEvidenceError("SNOWPACK executable is missing.")
    siblings = sorted(
        (path for path in exe.parent.iterdir() if path.is_file() and path.suffix.lower() == ".dll"),
        key=lambda path: path.name.casefold(),
    )
    if exe.suffix.lower() == ".exe" and not siblings:
        raise SnowpackRunEvidenceError(
            "Windows SNOWPACK closure has no adjacent runtime DLLs to inventory."
        )
    paths = [exe, *siblings]
    names = [path.name.casefold() for path in paths]
    if len(names) != len(set(names)):
        raise SnowpackRunEvidenceError("Binary closure contains case-insensitive duplicate names.")
    records = tuple(
        EvidenceFile(relative_path=path.name, bytes=path.stat().st_size, sha256=_file_sha256(path))
        for path in paths
    )
    digest = _sha256(_canonical([asdict(record) for record in records]))
    return records, digest


def _derive_input_inventory(
    input_files: Mapping[str, bytes], input_roles: Mapping[str, Sequence[str]]
) -> tuple[
    tuple[EvidenceFile, ...],
    dict[str, tuple[str, ...]],
    dict[str, str],
    str,
]:
    if set(input_roles) != REQUIRED_INPUT_ROLES:
        missing = sorted(REQUIRED_INPUT_ROLES - set(input_roles))
        extra = sorted(set(input_roles) - REQUIRED_INPUT_ROLES)
        raise SnowpackRunEvidenceError(
            f"SNOWPACK input roles are incomplete; missing={missing}, unexpected={extra}."
        )
    normalized_files: dict[str, bytes] = {}
    for relative, content in input_files.items():
        safe = _safe_relative(relative)
        if relative != safe:
            raise SnowpackRunEvidenceError(
                "Input paths must already use canonical forward-slash relative form."
            )
        if safe in normalized_files or not isinstance(content, bytes) or not content:
            raise SnowpackRunEvidenceError("Input files must have unique paths and non-empty bytes.")
        normalized_files[safe] = content
    normalized_roles: dict[str, tuple[str, ...]] = {}
    referenced: list[str] = []
    for role in sorted(REQUIRED_INPUT_ROLES):
        paths = tuple(sorted(_safe_relative(item) for item in input_roles[role]))
        if not paths or len(paths) != len(set(paths)):
            raise SnowpackRunEvidenceError(f"Input role {role!r} is empty or contains duplicates.")
        unknown = set(paths) - set(normalized_files)
        if unknown:
            raise SnowpackRunEvidenceError(
                f"Input role {role!r} references unknown files: {sorted(unknown)}."
            )
        normalized_roles[role] = paths
        referenced.extend(paths)
    if set(referenced) != set(normalized_files) or len(referenced) != len(set(referenced)):
        raise SnowpackRunEvidenceError(
            "Every input file must be assigned to exactly one explicit model-input role."
        )
    records = tuple(
        EvidenceFile(relative_path=path, bytes=len(content), sha256=_sha256(content))
        for path, content in sorted(normalized_files.items())
    )
    records_by_path = {record.relative_path: record for record in records}
    role_inventory_sha256 = {
        role: _sha256(
            _canonical([asdict(records_by_path[path]) for path in normalized_roles[role]])
        )
        for role in sorted(normalized_roles)
    }
    inventory = {
        "files": [asdict(record) for record in records],
        "roles": normalized_roles,
        "role_inventory_sha256": role_inventory_sha256,
    }
    return records, normalized_roles, role_inventory_sha256, _sha256(_canonical(inventory))


def _validate_forcing(content: bytes) -> None:
    try:
        lines = content.decode("ascii", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise SnowpackRunEvidenceError("Forcing SMET is not ASCII.") from exc
    if not lines or lines[0].strip() != "SMET 1.1 ASCII":
        raise SnowpackRunEvidenceError("Forcing is not a supported SMET 1.1 ASCII file.")
    try:
        header_index = lines.index("[HEADER]")
        data_index = lines.index("[DATA]")
    except ValueError as exc:
        raise SnowpackRunEvidenceError("Forcing SMET lacks HEADER or DATA sections.") from exc
    header: dict[str, str] = {}
    for raw in lines[header_index + 1 : data_index]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "=" not in raw:
            raise SnowpackRunEvidenceError("Forcing SMET contains a malformed header line.")
        key, value = (part.strip() for part in raw.split("=", 1))
        if key in header or not key or not value:
            raise SnowpackRunEvidenceError("Forcing SMET contains duplicate or empty headers.")
        header[key] = value
    if tuple(header.get("fields", "").split()) != SMET_FIELDS or header.get("tz") != "0":
        raise SnowpackRunEvidenceError("Forcing SMET fields or UTC declaration are not exact.")
    try:
        nodata = float(header["nodata"])
    except (KeyError, ValueError) as exc:
        raise SnowpackRunEvidenceError("Forcing SMET lacks a numeric nodata declaration.") from exc
    timestamps: list[datetime] = []
    for raw in lines[data_index + 1 :]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        tokens = raw.split()
        if len(tokens) != len(SMET_FIELDS):
            raise SnowpackRunEvidenceError("Forcing SMET row width is inconsistent.")
        try:
            timestamp = datetime.fromisoformat(tokens[0])
            values = tuple(float(token) for token in tokens[1:])
        except ValueError as exc:
            raise SnowpackRunEvidenceError("Forcing SMET contains invalid time or numeric data.") from exc
        if timestamp.tzinfo is not None or timestamp.minute or timestamp.second or timestamp.microsecond:
            raise SnowpackRunEvidenceError("Forcing timestamps must be naive exact hours under tz=0.")
        if not all(math.isfinite(value) for value in values):
            raise SnowpackRunEvidenceError("Forcing SMET contains non-finite data.")
        if any(value == nodata for value in values):
            raise SnowpackRunEvidenceError("Forcing SMET contains missing required values.")
        if timestamps and timestamp - timestamps[-1] != timedelta(hours=1):
            raise SnowpackRunEvidenceError("Forcing SMET cadence is not exactly hourly.")
        timestamps.append(timestamp)
    if len(timestamps) < 2:
        raise SnowpackRunEvidenceError("Representative-slope forcing needs at least two hourly rows.")


def build_snowpack_run_evidence(
    result: ExternalProcessResult,
    executable: str | Path,
    *,
    input_files: Mapping[str, bytes],
    input_roles: Mapping[str, Sequence[str]],
    parsed_output: ParsedSnowpackSmet,
    timeout_seconds: float,
    engine: str = "SNOWPACK 3.7.0",
    forcing_adapter_version: str = SMET_ADAPTER_VERSION,
) -> SnowpackRunEvidence:
    """Derive separate run-artifact and normalized scientific replay identities."""

    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or result.exit_code != 0
        or not engine.strip()
        or not result.version_output.strip()
    ):
        raise SnowpackRunEvidenceError("Only successful bounded runs can produce evidence.")
    binary_files, binary_inventory_sha256 = derive_binary_inventory(executable)
    exe = Path(executable).resolve()
    if result.executable_sha256 != _file_sha256(exe):
        raise SnowpackRunEvidenceError("Runner executable hash conflicts with binary inventory.")
    (
        input_records,
        normalized_roles,
        input_role_inventory_sha256,
        input_inventory_sha256,
    ) = _derive_input_inventory(input_files, input_roles)
    forcing_paths = normalized_roles["forcing"]
    if len(forcing_paths) != 1:
        raise SnowpackRunEvidenceError("Exactly one forcing SMET file is required.")
    _validate_forcing(input_files[forcing_paths[0]])
    if forcing_adapter_version != SMET_ADAPTER_VERSION:
        raise SnowpackRunEvidenceError("Forcing adapter version is not the reviewed adapter.")
    if not result.command_argv or Path(result.command_argv[0]).resolve() != exe:
        raise SnowpackRunEvidenceError("Runner command is not bound to the inventoried executable.")
    relative_arguments: list[str] = [exe.name]
    for argument in result.command_argv[1:]:
        path = Path(argument)
        if path.is_absolute() or ".." in path.parts:
            raise SnowpackRunEvidenceError("SNOWPACK arguments must not contain host-specific paths.")
        relative_arguments.append(path.as_posix())
    if _sha256(result.output) != parsed_output.raw_sha256:
        raise SnowpackRunEvidenceError("Parsed output is not the runner's captured raw output.")
    require_exact_cadence(parsed_output.timestamps_utc)

    replay_payload = {
        "schema_version": MODEL_INPUT_REPLAY_SCHEMA,
        "engine": engine,
        "executable_sha256": result.executable_sha256,
        "binary_inventory_sha256": binary_inventory_sha256,
        "input_inventory_sha256": input_inventory_sha256,
        "forcing_adapter_version": forcing_adapter_version,
        "output_parser_version": PARSER_VERSION,
        "relative_command_argv": tuple(relative_arguments),
        "normalized_output_sha256": parsed_output.normalized_sha256,
    }
    replay_sha256 = _sha256(_canonical(replay_payload))
    evidence_draft = {
        "schema_version": RUN_EVIDENCE_SCHEMA,
        "engine": engine,
        "executable_sha256": result.executable_sha256,
        "executable_version_output": result.version_output,
        "binary_files": tuple(asdict(record) for record in binary_files),
        "binary_inventory_sha256": binary_inventory_sha256,
        "input_files": tuple(asdict(record) for record in input_records),
        "input_roles": normalized_roles,
        "input_role_inventory_sha256": input_role_inventory_sha256,
        "input_inventory_sha256": input_inventory_sha256,
        "forcing_adapter_version": forcing_adapter_version,
        "output_parser_version": PARSER_VERSION,
        "relative_command_argv": tuple(relative_arguments),
        "timeout_seconds": timeout_seconds,
        "exit_code": result.exit_code,
        "stdout_sha256": _sha256(result.stdout),
        "stdout_bytes": len(result.stdout),
        "stderr_sha256": _sha256(result.stderr),
        "stderr_bytes": len(result.stderr),
        "raw_output_sha256": parsed_output.raw_sha256,
        "raw_output_bytes": len(result.output),
        "normalized_output_sha256": parsed_output.normalized_sha256,
        "model_input_replay_sha256": replay_sha256,
    }
    run_evidence_id = f"snowpack-run-{_sha256(_canonical(evidence_draft))}"
    return SnowpackRunEvidence(
        schema_version=RUN_EVIDENCE_SCHEMA,
        engine=engine,
        executable_sha256=result.executable_sha256,
        executable_version_output=result.version_output,
        binary_files=binary_files,
        binary_inventory_sha256=binary_inventory_sha256,
        input_files=input_records,
        input_roles=MappingProxyType(dict(normalized_roles)),
        input_role_inventory_sha256=MappingProxyType(dict(input_role_inventory_sha256)),
        input_inventory_sha256=input_inventory_sha256,
        forcing_adapter_version=forcing_adapter_version,
        output_parser_version=PARSER_VERSION,
        relative_command_argv=tuple(relative_arguments),
        timeout_seconds=timeout_seconds,
        exit_code=result.exit_code,
        stdout_sha256=evidence_draft["stdout_sha256"],
        stdout_bytes=len(result.stdout),
        stderr_sha256=evidence_draft["stderr_sha256"],
        stderr_bytes=len(result.stderr),
        raw_output_sha256=parsed_output.raw_sha256,
        raw_output_bytes=len(result.output),
        normalized_output_sha256=parsed_output.normalized_sha256,
        model_input_replay_sha256=replay_sha256,
        run_evidence_id=run_evidence_id,
    )
