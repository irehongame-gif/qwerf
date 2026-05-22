"""In-memory task manager for download jobs.

A "task" is one run of the ``ymsync.pipeline.process_track`` pipeline. Tasks
are keyed by ``track_id`` — kicking off a download for a track that's already
being processed (or already done) returns the existing task instead of starting
a duplicate.

The listener is meant for a single user on localhost, so a process-local dict
+ thread pool is plenty; we don't need a real queue or persistence.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from yandex_music import Client

from ymsync.config import Config
from ymsync.library import fetch_track
from ymsync.pipeline import TrackResult, TrackStage, process_track


@dataclass
class Task:
    """A single download/convert/export job."""

    id: str
    track_id: str
    stage: TrackStage = TrackStage.PENDING
    title: Optional[str] = None
    artists: list[str] = field(default_factory=list)
    error: Optional[str] = None
    downloaded_path: Optional[str] = None
    exported_path: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "track_id": self.track_id,
            "stage": self.stage.value,
            "title": self.title,
            "artists": self.artists,
            "error": self.error,
            "downloaded_path": self.downloaded_path,
            "exported_path": self.exported_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TaskManager:
    """Tracks running/finished tasks and runs them on a thread pool."""

    def __init__(
        self,
        cfg: Config,
        client: Client,
        *,
        max_workers: int = 2,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._lock = threading.Lock()
        self._tasks: Dict[str, Task] = {}              # id -> Task
        self._task_by_track: Dict[str, str] = {}       # track_id -> task id
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ymsync")

    # ----- public api ------------------------------------------------------

    def is_track_exported(self, track_id: str) -> Optional[Path]:
        """Return the export path if a track for that id is already exported.

        We can answer this without hitting Yandex by looking up the track once
        (cheap network call) and then checking the filesystem.
        """
        try:
            track = fetch_track(self._client, track_id)
        except Exception:
            return None
        from ymsync.naming import exported_filename

        candidate = self._cfg.export_dir / exported_filename(track)
        return candidate if candidate.is_file() else None

    def find_active_task_for_track(self, track_id: str) -> Optional[Task]:
        with self._lock:
            tid = self._task_by_track.get(track_id)
            if not tid:
                return None
            task = self._tasks.get(tid)
            if not task:
                return None
            # Recently-finished tasks are still useful for the UI to read,
            # so we return them too. Anything older than 5 minutes that's
            # finished is considered "expired" and we let a new task start.
            if task.stage in (TrackStage.DONE, TrackStage.SKIPPED, TrackStage.ERROR):
                if time.time() - task.updated_at > 300:
                    self._task_by_track.pop(track_id, None)
                    return None
            return task

    def enqueue(self, track_id: str) -> Task:
        """Start (or return existing) task for ``track_id``."""
        existing = self.find_active_task_for_track(track_id)
        if existing is not None:
            return existing

        task = Task(id=uuid.uuid4().hex, track_id=track_id)
        with self._lock:
            self._tasks[task.id] = task
            self._task_by_track[track_id] = task.id
        self._pool.submit(self._run, task)
        return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def list_recent(self, limit: int = 50) -> list[Task]:
        with self._lock:
            tasks = sorted(
                self._tasks.values(), key=lambda t: t.updated_at, reverse=True
            )
        return tasks[:limit]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # ----- internals -------------------------------------------------------

    def _run(self, task: Task) -> None:
        try:
            track = fetch_track(self._client, task.track_id)
        except Exception as exc:  # noqa: BLE001
            self._update(task, stage=TrackStage.ERROR, error=f"track lookup failed: {exc}")
            return

        with self._lock:
            task.title = track.title
            task.artists = [a.name for a in (track.artists or []) if a and a.name]
            task.updated_at = time.time()

        def _on_progress(result: TrackResult) -> None:
            self._update(
                task,
                stage=result.stage,
                error=result.error,
                downloaded_path=str(result.downloaded_path) if result.downloaded_path else None,
                exported_path=str(result.exported_path) if result.exported_path else None,
            )

        process_track(track, self._cfg, on_progress=_on_progress)

    def _update(
        self,
        task: Task,
        *,
        stage: Optional[TrackStage] = None,
        error: Optional[str] = None,
        downloaded_path: Optional[str] = None,
        exported_path: Optional[str] = None,
    ) -> None:
        with self._lock:
            if stage is not None:
                task.stage = stage
            if error is not None:
                task.error = error
            if downloaded_path is not None:
                task.downloaded_path = downloaded_path
            if exported_path is not None:
                task.exported_path = exported_path
            task.updated_at = time.time()
