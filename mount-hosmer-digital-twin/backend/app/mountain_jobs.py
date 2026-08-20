r"""Supervise the two subprocesses that turn an upload into a served mountain.

Both steps run in the *bake* environment, never in this process: the probe needs
rasterio to read the raster, and the bake needs the whole geospatial stack. That
is the same split :mod:`app.processing.runout.process` already maintains for the
external runout engines, and this module reuses its launcher rather than growing a
second one.

The bake is supervised rather than merely started. ``app.bake`` already prints
ordered ``[bake] ...`` markers for each stage, so progress comes from parsing the
output it already produces -- no new instrumentation, and a determinate stage list
instead of a spinner. Output is tee'd to ``<id>/bake.log`` as it arrives so a
failure is inspectable after the fact.

One bake runs at a time. The lock is process-local, which is the right scope for
the local launcher this feature ships in: a second concurrent request gets a 409
rather than two bakes competing for the same cores.

A deployment that ships the always-slim ``runtime`` image (see ``Dockerfile.backend``)
has no rasterio anywhere in its own process, so it cannot take the local-subprocess
path above at all. There, ``AVALANCHE_BAKE_WORKER_URL`` points at a separate
``bakeworker`` service -- built from the ``bake`` target, the only image in the fleet
with the geospatial stack -- and :func:`upload_available`, :func:`run_probe`, and
:func:`run_bake` each dispatch over HTTP instead. The bakeworker never sets that
variable on itself, so when its own copy of this module runs these same functions
in-process to actually do the work, they take the unmodified local-subprocess branch.
There is exactly one implementation of "run the probe" and "run the bake"; the HTTP
functions below are a thin network front door onto it, never a second copy.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from app.core.settings import Settings
from app.mountains import (
    MAX_ANALYSIS_CELLS,
    MountainRecord,
    bake_log_path,
    mountain_root,
    pack_path,
    source_root,
    update_record,
    utc_now_iso,
)

#: Ordered ``[bake]`` markers, matched as substrings against the marker lines that
#: ``app.bake`` prints. The label is what a progress UI shows.
BAKE_STAGES: tuple[tuple[str, str], ...] = (
    ("staging output", "Preparing"),
    ("computing terrain products", "Reading elevation"),
    ("writing elevation COG", "Building elevation image"),
    ("rendering terrain-RGB tiles", "Rendering terrain tiles"),
    ("rendering optional natural-colour", "Rendering imagery tiles"),
    ("saving", "Writing analysis layers"),
    ("building optional exposure", "Building exposure"),
    ("building grid->WGS84 lattice", "Finishing"),
)

PROBE_TIMEOUT_SECONDS = 300.0
BAKE_TIMEOUT_SECONDS = 3600.0

#: How often ``_run_bake_http`` polls the bakeworker's status endpoint. Mirrors
#: ``POLL_MS`` in ``frontend/src/components/MountainPicker.tsx`` -- same cadence,
#: the other side of the wire.
_BAKE_HTTP_POLL_SECONDS = 2.0

#: Serializes bakes within this process. See the module docstring.
_bake_lock = threading.Lock()


def _bake_worker_base_url() -> str | None:
    """The bakeworker service's base URL, or ``None`` for local subprocess dispatch.

    Read from the environment rather than :class:`Settings` for the same reason
    :func:`bake_python` reads ``AVALANCHE_BAKE_PYTHON`` that way: ``Settings`` is
    hashed into every frozen bake identity, and this value cannot change what a
    bake computes.
    """
    import os

    url = os.environ.get("AVALANCHE_BAKE_WORKER_URL", "").strip()
    return url.rstrip("/") or None


class BakeBusyError(RuntimeError):
    """Another mountain is baking. The caller should retry rather than queue."""


class MountainJobError(RuntimeError):
    """A user-visible failure from the probe or the bake."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProbeOutcome:
    ok: bool
    payload: dict[str, Any]

    @property
    def message(self) -> str:
        return str(self.payload.get("message", "The upload could not be validated."))

    @property
    def code(self) -> str:
        return str(self.payload.get("code", "probe_failed"))


def bake_python(settings: Settings) -> Path:
    """The interpreter that owns rasterio.

    Defaults to the interpreter running this process, which is correct for the
    local launcher: there, one environment holds both the service and the bake
    stack. A split deployment sets ``AVALANCHE_BAKE_PYTHON``, and a deployment
    that ships no geospatial stack at all simply has no working interpreter to
    point at -- which is what makes :func:`upload_available` report the feature as
    unavailable there instead of failing mid-request.

    Read from the environment rather than added to :class:`Settings` on purpose.
    ``core/settings.py`` is one of the files ``PROCESSING_MANIFEST_PATHS`` hashes
    to decide whether a frozen bake record is still current, so a field there --
    even one that cannot change what a bake computes -- would invalidate every
    published bake identity in the repository. ``service.assess_backend`` reads
    ``AVALANCHE_ASSESS_URL`` the same way, for the same reason.
    """
    import os

    configured = os.environ.get("AVALANCHE_BAKE_PYTHON", "").strip()
    return Path(configured) if configured else Path(sys.executable)


def upload_available(settings: Settings) -> tuple[bool, str]:
    """Whether this deployment can accept uploads, and why not when it cannot."""
    base_url = _bake_worker_base_url()
    if base_url is not None:
        return _upload_available_http(base_url)
    python = bake_python(settings)
    if not python.is_file():
        return False, f"No bake interpreter is configured or present at {python}."
    probe = subprocess.run(
        [str(python), "-I", "-c", "import rasterio, pyproj"],
        capture_output=True,
        check=False,
        timeout=60.0,
        shell=False,
    )
    if probe.returncode != 0:
        return False, (
            "Uploading a mountain needs the offline geospatial stack (rasterio, pyproj), "
            "which this deployment does not install. It is available in the local launcher."
        )
    return True, "The bake environment is available."


def _upload_available_http(base_url: str) -> tuple[bool, str]:
    """Ask the bakeworker service whether it is up and holds the geospatial stack.

    Timeout is short and deliberate: this backs ``GET /api/mountains``, a
    user-facing endpoint, and a parked or still-starting bakeworker must not hang
    it. Any failure to reach or parse the health check reads the same way as
    "not ready" -- the caller only needs a yes/no and a reason to show.
    """
    try:
        response = httpx.get(f"{base_url}/health", timeout=5.0)
        ready = response.status_code == 200 and bool(response.json().get("rasterio_importable"))
    except httpx.HTTPError:
        return False, (
            "The bake worker service is not reachable. It may be parked to save "
            "cost, or still starting."
        )
    except ValueError:
        return False, "The bake worker service reports it is not ready."
    if not ready:
        return False, "The bake worker service reports it is not ready."
    return True, "The bake worker service is available."


def _probe_script() -> Path:
    from app.processing import upload_probe

    return Path(upload_probe.__file__).resolve()


def run_probe(
    settings: Settings,
    *,
    mountain_id: str,
    name: str,
    provider: str,
    citation: str,
    licence: str,
    vertical_units_affirmed: bool,
    resolution_m: float | None,
) -> ProbeOutcome:
    """Validate the uploaded raster and synthesize its pack, out of process.

    Invoked by script path rather than ``-m``: ``run_isolated_worker`` launches
    with ``-I``, which drops ``PYTHONPATH``, so ``-m app.processing.upload_probe``
    cannot resolve. The worker puts the backend root back on ``sys.path`` itself,
    from the explicit ``--backend-root`` this passes it.
    """
    base_url = _bake_worker_base_url()
    if base_url is not None:
        return _run_probe_http(
            base_url,
            mountain_id=mountain_id,
            name=name,
            provider=provider,
            citation=citation,
            licence=licence,
            vertical_units_affirmed=vertical_units_affirmed,
            resolution_m=resolution_m,
        )
    arguments = [
        "--source-root", str(source_root(settings, mountain_id)),
        "--mountain-id", mountain_id,
        "--name", name,
        "--provider", provider,
        "--citation", citation,
        "--licence", licence,
        "--backend-root", str(settings.backend_root),
        "--max-cells", str(MAX_ANALYSIS_CELLS),
    ]
    if vertical_units_affirmed:
        arguments.append("--vertical-units-affirmed")
    if resolution_m is not None:
        arguments.extend(["--resolution-m", str(resolution_m)])

    try:
        completed = subprocess.run(
            [str(bake_python(settings)), "-I", str(_probe_script()), *arguments],
            capture_output=True,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MountainJobError(
            "probe_timeout", f"Validating the raster exceeded {PROBE_TIMEOUT_SECONDS:g} seconds."
        ) from exc
    except OSError as exc:
        raise MountainJobError("probe_launch_failed", f"The raster validator could not start: {exc}") from exc

    stdout = completed.stdout.decode("utf-8", errors="replace").strip()
    if not stdout:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[-800:]
        raise MountainJobError("probe_failed", f"The raster validator produced no result. {detail}")
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except ValueError as exc:
        raise MountainJobError("probe_failed", f"The raster validator returned invalid output: {exc}") from exc
    return ProbeOutcome(ok=bool(payload.get("ok")), payload=payload)


def _run_probe_http(
    base_url: str,
    *,
    mountain_id: str,
    name: str,
    provider: str,
    citation: str,
    licence: str,
    vertical_units_affirmed: bool,
    resolution_m: float | None,
) -> ProbeOutcome:
    """Delegate validation to the bakeworker service. Mirrors :func:`run_probe`'s
    contract exactly, so :func:`build_mountain` cannot tell which branch ran."""
    body = {
        "mountain_id": mountain_id,
        "name": name,
        "provider": provider,
        "citation": citation,
        "licence": licence,
        "vertical_units_affirmed": vertical_units_affirmed,
        "resolution_m": resolution_m,
    }
    try:
        response = httpx.post(f"{base_url}/probe", json=body, timeout=PROBE_TIMEOUT_SECONDS)
    except httpx.TimeoutException as exc:
        raise MountainJobError(
            "probe_timeout", f"Validating the raster exceeded {PROBE_TIMEOUT_SECONDS:g} seconds."
        ) from exc
    except httpx.HTTPError as exc:
        raise MountainJobError(
            "probe_launch_failed", f"The bake worker could not be reached: {exc}"
        ) from exc

    if response.status_code != 200:
        raise MountainJobError(*_worker_error(response, default_code="probe_failed"))

    payload = response.json()
    return ProbeOutcome(ok=bool(payload.get("ok")), payload=payload)


def _worker_error(response: httpx.Response, *, default_code: str) -> tuple[str, str]:
    """Parse a bakeworker error body of ``{"code", "message"}`` into (code, message).

    Falls back to a generic message if the body is missing or unparseable, so a
    proxy timeout or a bug on the other side still surfaces as a readable error
    here rather than a raw JSON-decode traceback.
    """
    try:
        detail = response.json()
    except ValueError:
        detail = {}
    code = str(detail.get("code") or default_code)
    message = str(detail.get("message") or f"The bake worker returned HTTP {response.status_code}.")
    return code, message


def run_bake(
    settings: Settings,
    *,
    mountain_id: str,
    on_stage: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Bake one uploaded mountain, streaming its stage markers to a callback.

    Raises :class:`BakeBusyError` if another bake holds the lock. Partial failure
    needs no handling here: ``app.bake`` stages into ``.baked-build-*`` and
    promotes only a validated result, so a crash leaves no half-baked mountain --
    only a staging directory, which ``mountains.sweep_orphaned_staging`` clears at
    startup.
    """
    base_url = _bake_worker_base_url()
    if base_url is not None:
        return _run_bake_http(base_url, mountain_id=mountain_id, on_stage=on_stage)
    if not _bake_lock.acquire(blocking=False):
        raise BakeBusyError("Another mountain is baking. Try again when it finishes.")
    try:
        root = mountain_root(settings, mountain_id)
        log_path = bake_log_path(settings, mountain_id)
        argv = [
            str(bake_python(settings)), "-m", "app.bake", "--force",
            "--pack", str(pack_path(settings, mountain_id)),
            "--data-root", str(source_root(settings, mountain_id)),
            "--runtime-root", str(root),
        ]
        # ``-m app.bake`` needs the backend root importable. This is a normal (not
        # isolated) launch precisely because it must inherit that path.
        environment = _bake_environment(settings)

        lines: list[str] = []
        stage_label = BAKE_STAGES[0][1]
        try:
            with log_path.open("w", encoding="utf-8") as log:
                process = subprocess.Popen(
                    argv,
                    cwd=str(settings.project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    shell=False,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    line = line.rstrip("\n")
                    lines.append(line)
                    log.write(line + "\n")
                    log.flush()
                    for marker, label in BAKE_STAGES:
                        if marker in line:
                            stage_label = label
                            if on_stage is not None:
                                on_stage(label)
                            break
                exit_code = process.wait(timeout=BAKE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise MountainJobError("bake_timeout", f"The bake exceeded {BAKE_TIMEOUT_SECONDS:g} seconds.") from exc
        except OSError as exc:
            raise MountainJobError("bake_launch_failed", f"The bake could not start: {exc}") from exc

        if exit_code != 0:
            raise MountainJobError("bake_failed", _last_error(lines))

        meta_path = root / "baked" / "meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MountainJobError("bake_incomplete", f"The bake wrote no readable meta.json: {exc}") from exc
        return {
            "stage": stage_label,
            "bake_sha256": str(meta.get("identity", {}).get("bake_sha256", "")),
            "warnings": [str(item) for item in meta.get("warnings", [])],
        }
    finally:
        _bake_lock.release()


def _run_bake_http(
    base_url: str, *, mountain_id: str, on_stage: Callable[[str], None] | None
) -> dict[str, Any]:
    """Start the bake on the bakeworker service and poll it to completion.

    The bakeworker starts the bake on a background thread and answers ``202``
    immediately (or ``409`` if it is already busy -- see ``main_bakeworker.start_bake``'s
    docstring for why that check is synchronous there), so this polls
    ``GET /bake/{id}`` -- at the same cadence the frontend polls this process's own
    status endpoint -- until a terminal state, calling ``on_stage`` on every stage
    change so :func:`build_mountain` keeps updating the registry exactly as it
    does for a local bake. The whole poll loop is bounded by
    :data:`BAKE_TIMEOUT_SECONDS`, measured with ``time.monotonic()``.
    """
    try:
        start_response = httpx.post(f"{base_url}/bake/{mountain_id}", timeout=10.0)
    except httpx.HTTPError as exc:
        raise MountainJobError(
            "bake_launch_failed", f"The bake worker could not be reached: {exc}"
        ) from exc

    if start_response.status_code == 409:
        raise BakeBusyError("Another mountain is baking. Try again when it finishes.")
    if start_response.status_code != 202:
        raise MountainJobError(*_worker_error(start_response, default_code="bake_launch_failed"))

    deadline = time.monotonic() + BAKE_TIMEOUT_SECONDS
    last_stage: str | None = None
    while True:
        try:
            status_response = httpx.get(f"{base_url}/bake/{mountain_id}", timeout=10.0)
        except httpx.HTTPError as exc:
            raise MountainJobError(
                "bake_launch_failed", f"The bake worker could not be reached: {exc}"
            ) from exc
        if status_response.status_code != 200:
            raise MountainJobError(*_worker_error(status_response, default_code="bake_failed"))

        payload = status_response.json()
        stage = payload.get("stage")
        if stage and stage != last_stage:
            last_stage = stage
            if on_stage is not None:
                on_stage(stage)

        status = payload.get("status")
        if status == "ready":
            return {
                "stage": stage,
                "bake_sha256": str(payload.get("bake_sha256") or ""),
                "warnings": [str(item) for item in payload.get("warnings") or ()],
            }
        if status == "failed":
            raise MountainJobError(
                payload.get("code") or "bake_failed", payload.get("error") or "The bake failed."
            )

        if time.monotonic() > deadline:
            raise MountainJobError(
                "bake_timeout", f"The bake exceeded {BAKE_TIMEOUT_SECONDS:g} seconds."
            )
        time.sleep(_BAKE_HTTP_POLL_SECONDS)


def _bake_environment(settings: Settings) -> dict[str, str]:
    import os

    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    backend = str(settings.backend_root)
    if backend not in existing.split(os.pathsep):
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (backend, existing)))
    # The bake resolves its own roots from the command line, but a stale
    # AVALANCHE_RUNTIME_ROOT in the parent environment would still be read by
    # Settings before --runtime-root overrides it. Clearing them removes any doubt
    # about which mountain a child process is writing.
    for name in ("AVALANCHE_RUNTIME_ROOT", "MOUNT_HOSMER_RUNTIME_ROOT", "AVALANCHE_DATA_ROOT",
                 "MOUNT_HOSMER_DATA_ROOT", "AVALANCHE_MOUNTAIN_PACK"):
        environment.pop(name, None)
    return environment


def _last_error(lines: list[str]) -> str:
    """The most informative line from a failed bake, for the status endpoint."""
    for line in reversed(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("[bake]"):
            return stripped[:500]
    return (lines[-1].strip()[:500] if lines else "The bake failed with no output.")


def build_mountain(
    settings: Settings,
    *,
    record: MountainRecord,
    provider: str,
    citation: str,
    licence: str,
    vertical_units_affirmed: bool,
    resolution_m: float | None,
) -> None:
    """Probe then bake, recording status at every transition.

    Runs on a background thread; every exit path writes a terminal status, so a
    mountain is never left reported as baking after this returns.
    """
    mountain_id = record.id
    try:
        outcome = run_probe(
            settings,
            mountain_id=mountain_id,
            name=record.name,
            provider=provider,
            citation=citation,
            licence=licence,
            vertical_units_affirmed=vertical_units_affirmed,
            resolution_m=resolution_m,
        )
        if not outcome.ok:
            update_record(
                settings, mountain_id, status="failed", error=outcome.message,
                stage=None, updated_at_utc=utc_now_iso(),
            )
            return

        payload = outcome.payload
        update_record(
            settings, mountain_id, status="baking", stage=BAKE_STAGES[0][1],
            pack_sha256=payload.get("pack_sha256"),
            assessable=bool(payload.get("assessable", False)),
            warnings=list(payload.get("warnings", ())),
            updated_at_utc=utc_now_iso(),
        )

        def note_stage(label: str) -> None:
            update_record(settings, mountain_id, stage=label, updated_at_utc=utc_now_iso())

        result = run_bake(settings, mountain_id=mountain_id, on_stage=note_stage)
        probe_warnings = list(payload.get("warnings", ()))
        merged = sorted({*probe_warnings, *result["warnings"]})
        update_record(
            settings, mountain_id, status="ready", stage=None, error=None,
            bake_sha256=result["bake_sha256"], warnings=merged,
            updated_at_utc=utc_now_iso(),
        )
    except (MountainJobError, BakeBusyError) as exc:
        update_record(
            settings, mountain_id, status="failed", error=str(exc),
            stage=None, updated_at_utc=utc_now_iso(),
        )
    except Exception as exc:  # never leave a mountain stuck in 'baking'
        update_record(
            settings, mountain_id, status="failed",
            error=f"Unexpected failure while building the mountain: {exc}",
            stage=None, updated_at_utc=utc_now_iso(),
        )
