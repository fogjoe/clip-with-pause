from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.processing import ClipJob, ClipProcessingResult, cleanup_expired_outputs


logger = logging.getLogger(__name__)


@dataclass
class ClipTask:
    task_id: str
    status: str
    progress: int
    message: str
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    result: ClipProcessingResult | None = None


ProcessFunction = Callable[[str, ClipJob, Path, int], ClipProcessingResult]


class TaskManager:
    def __init__(
        self,
        output_root: Path,
        ttl_seconds: int,
        max_workers: int,
        process_function: ProcessFunction,
    ) -> None:
        self.output_root = output_root
        self.ttl_seconds = ttl_seconds
        self.process_function = process_function
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.tasks: dict[str, ClipTask] = {}
        self.lock = threading.Lock()

    def create_task(self, job: ClipJob) -> ClipTask:
        cleanup_expired_outputs(self.output_root, self.ttl_seconds)
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        task = ClipTask(
            task_id=task_id,
            status="queued",
            progress=0,
            message="Task queued.",
            created_at=now,
            updated_at=now,
        )
        with self.lock:
            self.tasks[task_id] = task

        self.executor.submit(self._run_task, task_id, job)
        return task

    def get_task(self, task_id: str) -> ClipTask | None:
        with self.lock:
            return self.tasks.get(task_id)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _run_task(self, task_id: str, job: ClipJob) -> None:
        self._update_task(task_id, status="processing", progress=15, message="Downloading audio.")
        try:
            result = self.process_function(task_id, job, self.output_root, self.ttl_seconds)
            self._update_task(
                task_id,
                status="complete",
                progress=100,
                message="Clip ready.",
                result=result,
            )
        except Exception as exc:
            logger.exception("Clip task failed: %s", task_id)
            self._update_task(
                task_id,
                status="failed",
                progress=100,
                message="Clip processing failed.",
                error=str(exc),
            )

    def _update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        progress: int | None = None,
        message: str | None = None,
        error: str | None = None,
        result: ClipProcessingResult | None = None,
    ) -> None:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return
            if status is not None:
                task.status = status
            if progress is not None:
                task.progress = progress
            if message is not None:
                task.message = message
            if error is not None:
                task.error = error
            if result is not None:
                task.result = result
            task.updated_at = datetime.now(timezone.utc)
