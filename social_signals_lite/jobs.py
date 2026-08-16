"""In-memory background jobs — same poll contract as social-signals."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


@dataclass
class Job:
    id: str
    kind: str
    status: JobStatus = JobStatus.queued
    result: dict[str, Any] | None = None
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status.value,
            "created_at": self.created_at,
            "meta": self.meta,
        }
        if self.result is not None:
            out["result"] = self.result
        if self.error is not None:
            out["error"] = self.error
        return out


class JobStore:
    def __init__(self, workers: int = 2) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ss-lite")

    def submit(
        self,
        kind: str,
        work: Callable[[], dict[str, Any]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> Job:
        # Start as running so clients that only poll for "running" (harvest)
        # do not treat a brief queued window as a failed job.
        job = Job(
            id=str(uuid.uuid4()),
            kind=kind,
            status=JobStatus.running,
            meta=meta or {},
        )
        with self._lock:
            self._jobs[job.id] = job

        def _run() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    job.status = JobStatus.failed
                    job.error = f"{exc.__class__.__name__}: {exc}"
                return
            with self._lock:
                job.status = JobStatus.completed
                job.result = result

        self._pool.submit(_run)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)
