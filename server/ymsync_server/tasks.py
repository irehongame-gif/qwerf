"""In-memory job manager for the listener.

Three responsibilities:

* **Queue** — every enqueue returns immediately, even if the actual work has
  to wait for a worker slot. The popup can be closed and re-opened without
  killing the in-flight job.

* **Rate limit** — Yandex.Music is happy with 2 concurrent track downloads,
  YouTube tolerates more (we cap at 3). Each source gets its own
  ``ThreadPoolExecutor`` so the two pools don't starve each other.

* **Cancellation** — every job carries a :class:`threading.Event` that the
  pipeline + yt-dlp progress hook poll. The popup can hit
  ``POST /tasks/{id}/cancel`` at any point.

Jobs are deduplicated by ``(kind, dedup_key)`` — kicking off a download for
something already running (or already done) returns the existing record.

Albums and playlists are enqueued by spawning many individual jobs sharing a
``group_id`` so the popup can render them as one unit and cancel-all if the
user changes their mind.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from yandex_music import Client

from ymsync.config import Config
from ymsync.library import fetch_track
from ymsync.library_index import KIND_EXPORTED, LibraryIndex
from ymsync.naming import exported_filename
from ymsync.pipeline import TrackResult, TrackStage, is_terminal, process_track
from ymsync.yandex_album import extract_album_id, get_album_with_tracks


# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------


JobKind = str

KIND_YANDEX_TRACK: JobKind = "yandex_track"
KIND_YOUTUBE_AUDIO: JobKind = "youtube_audio"
KIND_YOUTUBE_VIDEO: JobKind = "youtube_video"

# Pool routing: which kinds share which worker pool.
_YANDEX_KINDS = {KIND_YANDEX_TRACK}
_YOUTUBE_KINDS = {KIND_YOUTUBE_AUDIO, KIND_YOUTUBE_VIDEO}


# ---------------------------------------------------------------------------
# Job record
# ---------------------------------------------------------------------------


@dataclass
class Task:
    id: str
    kind: JobKind
    dedup_key: str

    stage: TrackStage = TrackStage.PENDING

    title: Optional[str] = None
    artists: list[str] = field(default_factory=list)
    album: Optional[str] = None

    error: Optional[str] = None
    downloaded_path: Optional[str] = None
    exported_path: Optional[str] = None

    source_url: Optional[str] = None
    height: Optional[int] = None
    progress_pct: Optional[int] = None
    downloaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    source_codec: Optional[str] = None

    group_id: Optional[str] = None
    group_label: Optional[str] = None

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Runtime-only (not serialised):
    cancel_event: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "dedup_key": self.dedup_key,
            "stage": self.stage.value,
            "title": self.title,
            "artists": self.artists,
            "album": self.album,
            "error": self.error,
            "downloaded_path": self.downloaded_path,
            "exported_path": self.exported_path,
            "source_url": self.source_url,
            "height": self.height,
            "progress_pct": self.progress_pct,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "source_codec": self.source_codec,
            "group_id": self.group_id,
            "group_label": self.group_label,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "cancellable": not is_terminal(self.stage),
        }


# How long after a task finishes we keep returning the same record before
# letting a re-download create a new one.
_FINISHED_TTL_S = 300


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class TaskManager:
    """Tracks running/finished jobs and runs them on per-source pools."""

    def __init__(
        self,
        cfg: Config,
        client: Client,
        library_index: LibraryIndex,
        *,
        yandex_workers: int = 2,
        youtube_workers: int = 3,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._library_index = library_index

        self._lock = threading.Lock()
        self._tasks: Dict[str, Task] = {}
        self._by_kind_key: Dict[Tuple[JobKind, str], str] = {}
        self._futures: Dict[str, Future] = {}

        # Two named pools so 'list pools currently busy' matches 'list of
        # rate-limited sources' verbatim.
        self._yandex_pool = ThreadPoolExecutor(
            max_workers=yandex_workers, thread_name_prefix="ymsync-yandex"
        )
        self._youtube_pool = ThreadPoolExecutor(
            max_workers=youtube_workers, thread_name_prefix="ymsync-youtube"
        )

    # ------------------------------------------------------------------
    # Read-side helpers
    # ------------------------------------------------------------------

    def is_track_exported(self, track_id: str) -> Optional[Path]:
        """Fast path for the popup's per-result 'In library' badge.

        We accept *any* kind as proof of "I have this song" — see the note
        in :func:`ymsync_server.app._hit_to_model`: Music.app moves files
        out of the auto-import inbox almost immediately, so the persistent
        evidence lives in ``KIND_DOWNLOADED``.
        """
        try:
            track = fetch_track(self._client, track_id)
        except Exception:
            return None
        title = (track.title or "").strip() or None
        album = None
        if track.albums:
            album = (track.albums[0].title or "").strip() or None
        artists = [a.name for a in (track.artists or []) if a and a.name]

        rows = self._library_index.find_by_metadata(
            title, album, artists, kinds=None
        )
        for row in rows:
            if Path(row.path).is_file():
                return Path(row.path)

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
            # Errors and cancellations expire from dedup immediately so a
            # second click on the row acts as 'retry' and starts a fresh
            # task. Successful (DONE/SKIPPED) tasks stay around for a few
            # minutes so accidental double-clicks don't queue redundant work.
            if task.stage in (TrackStage.ERROR, TrackStage.CANCELLED):
                self._by_kind_key.pop((kind, dedup_key), None)
                return None
            if is_terminal(task.stage) and time.time() - task.updated_at > _FINISHED_TTL_S:
                self._by_kind_key.pop((kind, dedup_key), None)
                return None
            return task

    def get(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def list_recent(
        self, limit: int = 50, *, active_only: bool = False
    ) -> List[Task]:
        with self._lock:
            tasks = list(self._tasks.values())
        if active_only:
            # 'Active' means 'still relevant to the user' — anything in
            # progress, plus errors that haven't been acknowledged yet.
            # Done/skipped/cancelled are user-acknowledged terminal states
            # and the popup hides them.
            def _keep(s: TrackStage) -> bool:
                return not is_terminal(s) or s == TrackStage.ERROR
            tasks = [t for t in tasks if _keep(t.stage)]
        tasks.sort(key=lambda t: t.updated_at, reverse=True)
        return tasks[:limit]

    def list_group(self, group_id: str) -> List[Task]:
        with self._lock:
            return [t for t in self._tasks.values() if t.group_id == group_id]

    # ------------------------------------------------------------------
    # Single-job enqueues
    # ------------------------------------------------------------------

    def enqueue_yandex_track(
        self,
        track_id: str,
        *,
        group_id: Optional[str] = None,
        group_label: Optional[str] = None,
    ) -> Task:
        existing = self.find_active(KIND_YANDEX_TRACK, track_id)
        if existing is not None:
            return existing
        task = self._register(
            kind=KIND_YANDEX_TRACK,
            dedup_key=track_id,
            group_id=group_id,
            group_label=group_label,
        )
        self._submit(task, self._run_yandex_track, track_id)
        return task

    def enqueue_youtube_audio(
        self,
        url: str,
        *,
        group_id: Optional[str] = None,
        group_label: Optional[str] = None,
    ) -> Task:
        existing = self.find_active(KIND_YOUTUBE_AUDIO, url)
        if existing is not None:
            return existing
        task = self._register(
            kind=KIND_YOUTUBE_AUDIO,
            dedup_key=url,
            source_url=url,
            group_id=group_id,
            group_label=group_label,
        )
        self._submit(task, self._run_youtube_audio, url)
        return task

    def enqueue_youtube_video(
        self,
        url: str,
        height: int,
        *,
        group_id: Optional[str] = None,
        group_label: Optional[str] = None,
    ) -> Task:
        key = f"{url}#h={height}"
        existing = self.find_active(KIND_YOUTUBE_VIDEO, key)
        if existing is not None:
            return existing
        task = self._register(
            kind=KIND_YOUTUBE_VIDEO,
            dedup_key=key,
            source_url=url,
            height=height,
            group_id=group_id,
            group_label=group_label,
        )
        self._submit(task, self._run_youtube_video, url, height)
        return task

    # ------------------------------------------------------------------
    # Batch enqueues (album / playlist)
    # ------------------------------------------------------------------

    def enqueue_yandex_album(
        self, album_url_or_id: str
    ) -> Tuple[str, str, List[Task]]:
        """Enumerate the album and enqueue one Yandex track job per song.

        Returns ``(group_id, group_label, tasks)``.
        """
        album_id = extract_album_id(album_url_or_id)
        summary, tracks = get_album_with_tracks(self._client, album_id)
        group_id = f"yandex-album-{album_id}"
        group_label = summary.title
        tasks: List[Task] = []
        for track in tracks:
            tasks.append(
                self.enqueue_yandex_track(
                    str(track.id),
                    group_id=group_id,
                    group_label=group_label,
                )
            )
        return group_id, group_label, tasks

    def enqueue_youtube_playlist(
        self,
        playlist_url: str,
        *,
        mode: str = "audio",
        height: Optional[int] = None,
    ) -> Tuple[str, str, List[Task]]:
        """Enumerate a YouTube / YouTube Music playlist and enqueue one
        audio-or-video job per entry.

        Returns ``(group_id, group_label, tasks)``.

        For ``mode="video"`` you must pass ``height``; per-video fallback to
        a lower resolution happens automatically inside yt-dlp's format
        string (``bestvideo[height<=H]+bestaudio``).
        """
        from ymsync.youtube import fetch_playlist  # lazy: heavy import

        if mode not in {"audio", "video"}:
            raise ValueError(f"Unknown mode {mode!r}; expected 'audio'|'video'")
        if mode == "video" and (height is None or height <= 0):
            raise ValueError("`height` is required when mode='video'")

        info = fetch_playlist(playlist_url, self._cfg, library_index=self._library_index)
        group_id = f"yt-playlist-{uuid.uuid4().hex[:8]}"
        group_label = info.title
        tasks: List[Task] = []
        for entry in info.entries:
            if mode == "audio":
                t = self.enqueue_youtube_audio(
                    entry.url, group_id=group_id, group_label=group_label,
                )
            else:
                t = self.enqueue_youtube_video(
                    entry.url, int(height), group_id=group_id, group_label=group_label,
                )
            # Pre-fill display fields so the popup has something to show
            # before the job actually runs.
            with self._lock:
                if not t.title:
                    t.title = entry.title
                if not t.artists and entry.channel:
                    t.artists = [entry.channel]
                t.updated_at = time.time()
            tasks.append(t)
        return group_id, group_label, tasks

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def cancel(self, task_id: str) -> bool:
        """Cancel ``task_id``. Returns False if the task is already terminal
        or doesn't exist; True otherwise (cancellation may still take a
        moment to land for in-flight downloads).
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if is_terminal(task.stage):
                return False
            task.cancel_event.set()
            future = self._futures.get(task_id)

        # Not holding the lock while we touch the Future to avoid surprises
        # if the underlying ThreadPoolExecutor implementation grabs locks.
        if future is not None:
            # If the work hasn't started yet, this fully prevents it from
            # running. Otherwise the job stays running and will eventually
            # see cancel_event.
            future.cancel()

        # If cancel() succeeded for a not-yet-started job the worker won't
        # run our wrapper, so we eagerly mark it cancelled here. The worker
        # path also handles this safely (idempotent stage assignment).
        with self._lock:
            if task.stage == TrackStage.PENDING or task.stage == TrackStage.QUEUED:
                task.stage = TrackStage.CANCELLED
                task.error = "cancelled by user"
                task.updated_at = time.time()
        return True

    def cancel_group(self, group_id: str) -> int:
        """Cancel every active task with this ``group_id``. Returns the
        number of tasks that were eligible to cancel."""
        targets = [t.id for t in self.list_group(group_id) if not is_terminal(t.stage)]
        n = 0
        for tid in targets:
            if self.cancel(tid):
                n += 1
        return n

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        # Signal cancellation on every still-running task so workers exit
        # cleanly (rather than blocking on in-flight downloads).
        with self._lock:
            for t in self._tasks.values():
                if not is_terminal(t.stage):
                    t.cancel_event.set()
        self._yandex_pool.shutdown(wait=False, cancel_futures=True)
        self._youtube_pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _pool_for(self, kind: JobKind) -> ThreadPoolExecutor:
        if kind in _YANDEX_KINDS:
            return self._yandex_pool
        if kind in _YOUTUBE_KINDS:
            return self._youtube_pool
        raise ValueError(f"Unknown job kind {kind!r}")

    def _register(
        self,
        *,
        kind: JobKind,
        dedup_key: str,
        source_url: Optional[str] = None,
        height: Optional[int] = None,
        group_id: Optional[str] = None,
        group_label: Optional[str] = None,
    ) -> Task:
        task = Task(
            id=uuid.uuid4().hex,
            kind=kind,
            dedup_key=dedup_key,
            source_url=source_url,
            height=height,
            group_id=group_id,
            group_label=group_label,
        )
        with self._lock:
            self._tasks[task.id] = task
            self._by_kind_key[(kind, dedup_key)] = task.id
        return task

    def _submit(self, task: Task, fn: Callable, *args) -> None:
        # Mark queued before submitting so the popup sees the right stage
        # even for jobs that wait a moment for a slot.
        self._update(task, stage=TrackStage.QUEUED)
        future = self._pool_for(task.kind).submit(self._wrap, task, fn, *args)
        with self._lock:
            self._futures[task.id] = future

    def _wrap(self, task: Task, fn: Callable, *args) -> None:
        # The Future may have been cancelled before our worker picked it up;
        # cancel_event was set if so.
        if task.cancel_event.is_set():
            self._update(task, stage=TrackStage.CANCELLED, error="cancelled by user")
            return
        try:
            fn(task, *args)
        finally:
            # Detach future so we don't leak references for finished jobs.
            with self._lock:
                self._futures.pop(task.id, None)

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
            if track.albums:
                task.album = track.albums[0].title
                task.source_url = (
                    f"https://music.yandex.ru/album/{track.albums[0].id}"
                    f"/track/{track.id}"
                )
            else:
                task.source_url = f"https://music.yandex.ru/track/{track.id}"
            task.updated_at = time.time()

        process_track(
            track,
            self._cfg,
            library_index=self._library_index,
            on_progress=self._make_progress(task),
            cancel_event=task.cancel_event,
        )

    # -- runner: youtube audio --------------------------------------------

    def _run_youtube_audio(self, task: Task, url: str) -> None:
        from ymsync.youtube import download_audio, fetch_info

        try:
            info = fetch_info(url, self._cfg, library_index=self._library_index)
        except Exception as exc:  # noqa: BLE001
            self._update(task, stage=TrackStage.ERROR, error=f"yt-dlp info failed: {exc}")
            return

        with self._lock:
            task.title = info.title
            task.artists = [info.channel] if info.channel else []
            task.updated_at = time.time()

        download_audio(
            url,
            self._cfg,
            library_index=self._library_index,
            on_progress=self._make_progress(task),
            cancel_event=task.cancel_event,
        )

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

        download_video(
            url,
            height,
            self._cfg,
            on_progress=self._make_progress(task),
            cancel_event=task.cancel_event,
        )

    # -- shared progress callback ------------------------------------------

    def _make_progress(self, task: Task) -> Callable[[TrackResult], None]:
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
                source_codec=result.extra.get("source_codec"),
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
        source_codec: Optional[str] = None,
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
            if source_codec is not None:
                task.source_codec = source_codec
            task.updated_at = time.time()
