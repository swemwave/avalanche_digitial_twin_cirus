"""Background job runner.

A terrain rebuild takes 90 seconds and an advanced simulation can take longer.
Neither may block an HTTP request, so both run here and the client polls.

This is a thread pool backed by the `jobs` table, **not** Celery. The reason is
the same one that drove the SQLite decision: the primary delivery mode is a
double-clicked `.exe` with no message broker and no network. A Celery dependency
would make the flagship user journey impossible.

What matters is that the *contract* is broker-agnostic -- queued/running/
succeeded/failed, progress, failure reason, idempotency key, duration, result id.
Swapping in Celery or RQ later means reimplementing `submit` and `_execute`, and
nothing else in the application changes.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select

from app.core.settings import Settings
from app.storage.database import session_scope
from app.storage.models import Job

logger = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = {SUCCEEDED, FAILED, CANCELLED}


class JobsDisabledError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, settings.max_job_workers), thread_name_prefix="avalanche-job"
        )
        self._futures: dict[str, Future] = {}
        # Guards against two identical jobs starting concurrently before either
        # has written its idempotency key.
        self._lock = threading.Lock()
        self._shutdown = False

    # --- Submission -----------------------------------------------------------

    def submit(
        self,
        job_type: str,
        handler: Callable[..., dict[str, Any]],
        parameters: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Queue a job. Returns immediately with the job record.

        If ``idempotency_key`` matches a job that is queued, running, or already
        succeeded, that job is returned instead of starting a duplicate. Two
        clicks on "Run simulation" must not run two simulations.
        """
        if not self.settings.jobs_enabled:
            raise JobsDisabledError(
                "Background jobs are disabled (AVALANCHE_JOBS_ENABLED=false). Run the equivalent "
                "CLI command instead."
            )
        if self._shutdown:
            raise RuntimeError("The job runner is shutting down and is not accepting work.")

        with self._lock:
            if idempotency_key:
                existing = self._find_by_key(idempotency_key)
                if existing is not None and existing["state"] != FAILED:
                    logger.info(
                        "Job with idempotency key %s already exists (%s, %s); returning it.",
                        idempotency_key,
                        existing["job_id"],
                        existing["state"],
                    )
                    existing["deduplicated"] = True
                    return existing
                if existing is not None:
                    # The previous attempt failed. Free the key so a retry can run.
                    self._clear_key(idempotency_key)

            job_id = f"JOB_{utcnow().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
            with session_scope(self.settings) as session:
                session.add(
                    Job(
                        job_id=job_id,
                        job_type=job_type,
                        idempotency_key=idempotency_key,
                        state=QUEUED,
                        progress=0,
                        progress_message="Queued",
                        parameters_json=Job.dumps(parameters or {}),
                        model_version=self.settings.model_version,
                        correlation_id=correlation_id,
                    )
                )

            future = self._executor.submit(
                self._execute, job_id, handler, parameters or {}
            )
            self._futures[job_id] = future

        return self.get(job_id) or {"job_id": job_id, "state": QUEUED}

    # --- Execution ------------------------------------------------------------

    def _execute(
        self, job_id: str, handler: Callable[..., dict[str, Any]], parameters: dict[str, Any]
    ) -> dict[str, Any]:
        started = time.perf_counter()
        self._update(job_id, state=RUNNING, progress=1, message="Starting", started_utc=utcnow())

        def progress(percent: int, message: str) -> None:
            self._update(job_id, progress=int(percent), message=message)

        try:
            # Settings is injected rather than carried in `parameters`, because
            # `parameters` is persisted as JSON on the job row. Serialising a
            # Settings object there would stringify it via `default=str` -- no crash,
            # but every stored job would carry a blob of absolute filesystem paths,
            # and the record of what a job was asked to do would stop being a clean,
            # replayable set of arguments.
            result = handler(settings=self.settings, progress=progress, **parameters)
        except Exception as exc:  # noqa: BLE001 - the failure reason must reach the user
            duration = time.perf_counter() - started
            detail = f"{type(exc).__name__}: {exc}"
            logger.exception("Job %s failed", job_id)
            self._update(
                job_id,
                state=FAILED,
                progress=100,
                message="Failed",
                failure_reason=detail,
                # The traceback goes to the log, not to the API response.
                result={"traceback": traceback.format_exc()[-4000:]},
                finished_utc=utcnow(),
                duration_seconds=duration,
            )
            raise

        duration = time.perf_counter() - started
        result_id = (
            result.get("simulation_id")
            or result.get("analysis_id")
            or result.get("run_id")
            if isinstance(result, dict)
            else None
        )
        layer_ids = (
            [layer.get("id") for layer in result.get("layers", [])]
            if isinstance(result, dict)
            else []
        )

        self._update(
            job_id,
            state=SUCCEEDED,
            progress=100,
            message="Complete",
            result=_summarize(result),
            result_id=result_id,
            layer_ids=layer_ids,
            finished_utc=utcnow(),
            duration_seconds=duration,
        )
        return result

    def _update(
        self,
        job_id: str,
        *,
        state: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        failure_reason: str | None = None,
        result: dict[str, Any] | None = None,
        result_id: str | None = None,
        layer_ids: list[str] | None = None,
        started_utc: datetime | None = None,
        finished_utc: datetime | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        with session_scope(self.settings) as session:
            job = session.scalar(select(Job).where(Job.job_id == job_id))
            if job is None:
                return
            if state is not None:
                job.state = state
            if progress is not None:
                job.progress = max(0, min(100, progress))
            if message is not None:
                job.progress_message = message[:255]
            if failure_reason is not None:
                job.failure_reason = failure_reason
            if result is not None:
                job.result_json = Job.dumps(result)
            if result_id is not None:
                job.result_id = result_id
            if layer_ids is not None:
                job.generated_layer_ids_json = Job.dumps(layer_ids)
            if started_utc is not None:
                job.started_utc = started_utc
            if finished_utc is not None:
                job.finished_utc = finished_utc
            if duration_seconds is not None:
                job.duration_seconds = round(duration_seconds, 3)

    # --- Queries --------------------------------------------------------------

    def get(self, job_id: str) -> dict[str, Any] | None:
        with session_scope(self.settings) as session:
            job = session.scalar(select(Job).where(Job.job_id == job_id))
            return _serialize(job) if job else None

    def list(self, limit: int = 50, state: str | None = None) -> list[dict[str, Any]]:
        with session_scope(self.settings) as session:
            query = select(Job).order_by(Job.created_utc.desc()).limit(limit)
            if state:
                query = query.where(Job.state == state)
            return [_serialize(job) for job in session.scalars(query)]

    def _find_by_key(self, key: str) -> dict[str, Any] | None:
        with session_scope(self.settings) as session:
            job = session.scalar(select(Job).where(Job.idempotency_key == key))
            return _serialize(job) if job else None

    def _clear_key(self, key: str) -> None:
        with session_scope(self.settings) as session:
            job = session.scalar(select(Job).where(Job.idempotency_key == key))
            if job is not None:
                job.idempotency_key = None

    def cancel(self, job_id: str) -> bool:
        """Cancel a job that has not started. A running job cannot be interrupted."""
        future = self._futures.get(job_id)
        if future is None or not future.cancel():
            return False
        self._update(job_id, state=CANCELLED, message="Cancelled before it started")
        return True

    def shutdown(self, wait: bool = True) -> None:
        self._shutdown = True
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def reset_orphans(self) -> int:
        """Fail jobs left RUNNING by a process that died. Called at startup.

        Without this a crash leaves a job stuck at "running, 40%" forever, and the
        UI waits on something that will never finish.
        """
        with session_scope(self.settings) as session:
            orphans = list(session.scalars(select(Job).where(Job.state.in_([QUEUED, RUNNING]))))
            for job in orphans:
                job.state = FAILED
                job.failure_reason = (
                    "The application restarted while this job was in progress. It did not complete. "
                    "Re-submit it."
                )
                job.finished_utc = utcnow()
                job.progress_message = "Interrupted by restart"
            return len(orphans)


def _summarize(result: Any) -> dict[str, Any]:
    """Store a compact result on the job; the full payload lives on disk."""
    if not isinstance(result, dict):
        return {"value": str(result)[:500]}
    keep = {
        "analysis_id",
        "simulation_id",
        "hazard_score",
        "confidence_score",
        "duration_seconds",
        "mode",
        "simulation_mode",
        "release_size",
        "layer_count",
    }
    summary = {key: value for key, value in result.items() if key in keep}
    if "assessment" in result:
        summary["assessment"] = result["assessment"]
    if "release_zones" in result:
        summary["release_zone_count"] = (result["release_zones"] or {}).get("zone_count", 0)
    if "warnings" in result:
        summary["warning_count"] = len(result["warnings"])
    return summary


def _serialize(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "job_type": job.job_type,
        "state": job.state,
        "progress": job.progress,
        "progress_message": job.progress_message,
        "parameters": Job.loads(job.parameters_json),
        "result": Job.loads(job.result_json),
        "result_id": job.result_id,
        "generated_layer_ids": Job.loads(job.generated_layer_ids_json) or [],
        "failure_reason": job.failure_reason,
        "model_version": job.model_version,
        "duration_seconds": job.duration_seconds,
        "correlation_id": job.correlation_id,
        "created_utc": job.created_utc.isoformat() if job.created_utc else None,
        "started_utc": job.started_utc.isoformat() if job.started_utc else None,
        "finished_utc": job.finished_utc.isoformat() if job.finished_utc else None,
    }


_runner: JobRunner | None = None


def get_runner(settings: Settings) -> JobRunner:
    global _runner
    if _runner is None:
        _runner = JobRunner(settings)
    return _runner


def shutdown_runner() -> None:
    global _runner
    if _runner is not None:
        _runner.shutdown(wait=False)
        _runner = None
