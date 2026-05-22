"""In-memory job manager for the listener.

A *job* is one run of a download pipeline — Yandex sync, YouTube audio, or
YouTube video. All three share the same lifecycle stages, the same task
record shape, and the same ``/tasks/{id}`` polling protocol, so the popup
only ever needs to know about ``Task.id`` + ``Task.stage``.

Jobs are deduplicated by ``(kind, dedup_key)`` — kicking off a download for
something already running (or already done) returns the existing job instead
of starting a duplicate.

The listener is meant for a single user on localhost, so a process-local dict
+ thread pool is plenty; no real queue or persistence.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from yandex_music import Client

from ymsync.config import Config
from ymsync.library import fetch_track
from ymsync.naming import exported_filename
from ymsync.pipeline import TrackResult, TrackStage, process_track


# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------


JobKind = str  # "yandex_track" | "youtube_audio" | "youtube_video"

KIND_YANDEX_TRACK: JobKind = "yandex_track"
KIND_YOUTUBE_AUDIO: JobKind = "youtube_audio"
KIND_YOUTUBE_VIDEO: JobKind = "youtube_video"


# ---------------------------------------------------------------------------
# Job record
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """A single download/convert/export job (any kind)."""

    id: str
    kind: JobKind
    dedup_key: str

    stage: TrackStage = TrackStage.PENDING

    # Display fields, populated as soon as we know enough:
    title: Optional[str] = None
    artists: list[str] = field(default_factory=list)  # or [channel] for YouTube

    # Outputs:
    error: Optional[str] = None
    downloaded_path: Optional[str] = None
    exported_path: Optional[str] = None

    # Per-kind context that the popup may want to display:
    source_url: Optional[str] = None
    height: Optional[int] = None  # for youtube_video
    progress_pct: Optional[int] = None
    downloaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "dedup_key": self.dedup_key,
            "stage": self.stage.value,
            "title": self.title,
            "artists": self.artists,
            "error": self.error,
            "downloaded_path": self.downloaded_path,
            "exported_path": self.exported_path,
            "source_url": self.source_url,
            "height": self.height,
            "progress_pct": self.progress_pct,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# How long after a task finishes we keep returning the same record before
# letting a re-download create a new one.
_FINISHED_TTL_S = 300


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class TaskManager:
    """Tracks running/finished jobs and runs them on a thread pool."""

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
        self._tasks: Dict[str, Task] = {}                          # id -> Task
        self._by_kind_key: Dict[Tuple[JobKind, str], str] = {}     # (kind,key) -> id
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="ymsync"
        )

    # ----- read-side helpers ---------------------------------------------

    def is_track_exported(self, track_id: str) -> Optional[Path]:
        """Return the export path if the Yandex track is already in the library."""
        try:
            track = fetch_track(self._client, track_id)
        except Exception:
            return None
        candidate = self._cfg.export_dir / exported_filename(track)
        return candidate if candidate.is_file() else None

    def find_active(self, kind: JobKind, dedup_key: str) -> Optional[Task]:
        with self._lock:
            tid = self._by_kind_key.get((kind, dedup_key))
            if not tid:
                return None
            task = self._tasks.get(tid)
            if not task:
                return None
            if task.stage in (
                TrackStage.DONE,
                TrackStage.SKIPPED,
                TrackStage.ERROR,
            ) and time.time() - task.updated_at > _FINISHED_TTL_S:
                self._by_kind_key.pop((kind, dedup_key), None)
                return None
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

    # ----- enqueue (one method per kind) ---------------------------------

    def enqueue_yandex_track(self, track_id: str) -> Task:
        existing = self.find_active(KIND_YANDEX_TRACK, track_id)
        if existing is not None:
            return existing
        task = self._register(
            kind=KIND_YANDEX_TRACK,
            dedup_key=track_id,
        )
        self._pool.submit(self._run_yandex_track, task, track_id)
        return task

    def enqueue_youtube_audio(self, url: str) -> Task:
        existing = self.find_active(KIND_YOUTUBE_AUDIO, url)
        if existing is not None:
            return existing
        task = self._register(
            kind=KIND_YOUTUBE_AUDIO,
            dedup_key=url,
            source_url=url,
        )
        self._pool.submit(self._run_youtube_audio, task, url)
        return task

    def enqueue_youtube_video(self, url: str, height: int) -> Task:
        key = f"{url}#h={height}"
        existing = self.find_active(KIND_YOUTUBE_VIDEO, key)
        if existing is not None:
            return existing
        task = self._register(
            kind=KIND_YOUTUBE_VIDEO,
            dedup_key=key,
            source_url=url,
            height=height,
        )
        self._pool.submit(self._run_youtube_video, task, url, height)
        return task

    # ----- internals ------------------------------------------------------

    def _register(
        self,
        *,
        kind: JobKind,
        dedup_key: str,
        source_url: Optional[str] = None,
        height: Optional[int] = None,
    ) -> Task:
        task = Task(
            id=uuid.uuid4().hex,
            kind=kind,
            dedup_key=dedup_key,
            source_url=source_url,
            height=height,
        )
        with self._lock:
            self._tasks[task.id] = task
            self._by_kind_key[(kind, dedup_key)] = task.id
        return task

    # -- runner: yandex track ---------------------------------------------

    def _run_yandex_track(self, task: Task, track_id: str) -> None:
        try:
            track = fetch_track(self._client, track_id)
        except Exception as exc:  # noqa: BLE001
            self._update(task, stage=TrackStage.ERROR, error=f"track lookup failed: {exc}")
            return

        with self._lock:
            task.title = track.title
            task.artists = [a.name for a in (track.artists or []) if a and a.name]
            task.source_url = (
                f"https://music.yandex.ru/album/{track.albums[0].id}/track/{track.id}"
                if track.albums else f"https://music.yandex.ru/track/{track.id}"
            )
            task.updated_at = time.time()

        process_track(track, self._cfg, on_progress=self._make_progress(task))

    # -- runner: youtube audio --------------------------------------------

    def _run_youtube_audio(self, task: Task, url: str) -> None:
        # Lazy import keeps yt-dlp out of the critical import path.
        from ymsync.youtube import download_audio, fetch_info

        try:
            info = fetch_info(url, self._cfg)
        except Exception as exc:  # noqa: BLE001
            self._update(task, stage=TrackStage.ERROR, error=f"yt-dlp info failed: {exc}")
            return

        with self._lock:
            task.title = info.title
            task.artists = [info.channel] if info.channel else []
            task.updated_at = time.time()

        download_audio(url, self._cfg, on_progress=self._make_progress(task))

    # -- runner: youtube video --------------------------------------------

    def _run_youtube_video(self, task: Task, url: str, height: int) -> None:
        from ymsync.youtube import download_video, fetch_info

        try:
            info = fetch_info(url, self._cfg)
        except Exception as exc:  # noqa: BLE001
            self._update(task, stage=TrackStage.ERROR, error=f"yt-dlp info failed: {exc}")
            return

        with self._lock:
            task.title = info.title
            task.artists = [info.channel] if info.channel else []
            task.updated_at = time.time()

        download_video(url, height, self._cfg, on_progress=self._make_progress(task))

    # -- shared progress callback ------------------------------------------

    def _make_progress(self, task: Task) -> Callable[[TrackResult], None]:
        """Project a :class:`TrackResult` from any pipeline onto our :class:`Task`."""

        def _on(result: TrackResult) -> None:
            self._update(
                task,
                stage=result.stage,
                error=result.error,
                downloaded_path=str(result.downloaded_path) if result.downloaded_path else None,
                exported_path=str(result.exported_path) if result.exported_path else None,
                progress_pct=result.extra.get("progress_pct"),
                downloaded_bytes=result.extra.get("downloaded_bytes"),
                total_bytes=result.extra.get("total_bytes"),
            )

        return _on

    def _update(
        self,
        task: Task,
        *,
        stage: Optional[TrackStage] = None,
        error: Optional[str] = None,
        downloaded_path: Optional[str] = None,
        exported_path: Optional[str] = None,
        progress_pct: Optional[int] = None,
        downloaded_bytes: Optional[int] = None,
        total_bytes: Optional[int] = None,
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
            if progress_pct is not None:
                task.progress_pct = progress_pct
            if downloaded_bytes is not None:
                task.downloaded_bytes = downloaded_bytes
            if total_bytes is not None:
                task.total_bytes = total_bytes
            task.updated_at = time.time()
