"""FastAPI app exposing search / download / status to the Chrome extension.

The app is bound to localhost only — it holds the Yandex token. CORS is open
for ``chrome-extension://*`` so the popup can call us via ``fetch``.

Routes:

================  ===================================  ===============================
Method            Path                                 Purpose
================  ===================================  ===============================
GET               /health                              liveness check
GET               /search?q=…                          Yandex.Music search
GET               /tracks/{track_id}                   "is this Yandex track exported?"
POST              /download                            start Yandex track download
GET               /yandex/track-info?…                 single-track info for context card
GET               /youtube/info?url=…                  yt-dlp metadata + heights
POST              /youtube/audio                       start YouTube audio download
POST              /youtube/video                       start YouTube video download
GET               /tasks/{task_id}                     poll any job
GET               /tasks                               list recent jobs
================  ===================================  ===============================
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ymsync.client import build_client
from ymsync.config import Config, load_config
from ymsync.library import fetch_track
from ymsync.naming import stem_from_metadata
from ymsync.search import hit_from_track, search_tracks

from ymsync_server.tasks import TaskManager


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------

_YANDEX_TRACK_RE = re.compile(
    r"music\.yandex\.\w+/(?:album/(?P<album>\d+)/)?track/(?P<track>\d+)"
)


def _extract_yandex_track_id(url_or_id: str) -> str:
    if url_or_id.isdigit():
        return url_or_id
    m = _YANDEX_TRACK_RE.search(url_or_id)
    if m is None:
        raise HTTPException(
            status_code=400,
            detail="Could not extract a Yandex track id from the given value.",
        )
    return m.group("track")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SearchHitModel(BaseModel):
    id: str
    title: str
    artists: List[str]
    album: Optional[str] = None
    album_id: Optional[str] = None
    cover_url: Optional[str] = None
    duration_ms: int = 0
    yandex_url: str
    available: bool = True
    already_exported: bool = False


class TrackStatusModel(BaseModel):
    track_id: str
    exported: bool
    exported_path: Optional[str] = None


class StartDownloadRequest(BaseModel):
    track_id: str = Field(..., description="Numeric Yandex.Music track id")


class YandexTrackInfoQuery(BaseModel):
    url: Optional[str] = None
    track_id: Optional[str] = None


class YouTubeAudioRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")


class YouTubeVideoRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    height: int = Field(
        ..., ge=144, le=4320, description="Maximum video height (e.g. 1080)"
    )


class YoutubeInfoModel(BaseModel):
    url: str
    title: str
    channel: str
    duration_s: int
    thumbnail_url: Optional[str] = None
    available_heights: List[int]
    audio_already_exported: bool = False
    expected_export_path: Optional[str] = None
    expected_video_path_template: Optional[str] = None


class TaskModel(BaseModel):
    id: str
    kind: str
    dedup_key: str
    stage: str
    title: Optional[str] = None
    artists: List[str] = []
    error: Optional[str] = None
    downloaded_path: Optional[str] = None
    exported_path: Optional[str] = None
    source_url: Optional[str] = None
    height: Optional[int] = None
    progress_pct: Optional[int] = None
    downloaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    created_at: float
    updated_at: float


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(cfg: Optional[Config] = None) -> FastAPI:
    """Build the FastAPI app. ``cfg`` is loaded from disk if not provided."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ARG001
        nonlocal cfg
        if cfg is None:
            cfg = load_config()
        cfg.ensure_dirs()
        client = build_client(cfg.token)
        manager = TaskManager(cfg, client)
        app.state.cfg = cfg
        app.state.client = client
        app.state.tasks = manager
        try:
            yield
        finally:
            manager.shutdown()

    app = FastAPI(
        title="ymsync-server",
        version="0.2.0",
        description="Local listener for the qwerf Chrome extension.",
        lifespan=lifespan,
    )

    # The extension calls us from chrome-extension://<id>; we don't know the
    # id ahead of time, so allow any chrome-extension origin + localhost.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(chrome-extension://.*|http://(127\.0\.0\.1|localhost)(:\d+)?)$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # -----------------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": "0.2.0"}

    # -----------------------------------------------------------------------
    # Yandex.Music — search / track info / download
    # -----------------------------------------------------------------------

    @app.get("/search", response_model=List[SearchHitModel])
    def search(
        q: str = Query(..., min_length=1, description="search query"),
        limit: int = Query(20, ge=1, le=50),
    ) -> List[SearchHitModel]:
        cfg_local: Config = app.state.cfg
        try:
            hits = search_tracks(app.state.client, q, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Yandex search failed: {exc}")
        return [_search_hit_to_model(h, cfg_local) for h in hits]

    @app.get("/tracks/{track_id}", response_model=TrackStatusModel)
    def track_status(track_id: str) -> TrackStatusModel:
        manager: TaskManager = app.state.tasks
        path = manager.is_track_exported(track_id)
        return TrackStatusModel(
            track_id=track_id,
            exported=path is not None,
            exported_path=str(path) if path else None,
        )

    @app.get("/yandex/track-info", response_model=SearchHitModel)
    def yandex_track_info(
        url: Optional[str] = Query(None, description="music.yandex.ru track URL"),
        track_id: Optional[str] = Query(None, description="numeric track id"),
    ) -> SearchHitModel:
        if not url and not track_id:
            raise HTTPException(status_code=400, detail="Pass `url` or `track_id`.")
        resolved_id = track_id or _extract_yandex_track_id(url or "")
        try:
            track = fetch_track(app.state.client, resolved_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Yandex lookup failed: {exc}")
        return _search_hit_to_model(hit_from_track(track), app.state.cfg)

    @app.post("/download", response_model=TaskModel)
    def start_yandex_download(payload: StartDownloadRequest) -> TaskModel:
        manager: TaskManager = app.state.tasks
        task = manager.enqueue_yandex_track(payload.track_id)
        return TaskModel(**task.to_dict())

    # -----------------------------------------------------------------------
    # YouTube — info / audio / video
    # -----------------------------------------------------------------------

    @app.get("/youtube/info", response_model=YoutubeInfoModel)
    def youtube_info(
        url: str = Query(..., description="YouTube video URL"),
    ) -> YoutubeInfoModel:
        from ymsync.youtube import fetch_info  # lazy: heavy yt-dlp import

        cfg_local: Config = app.state.cfg
        try:
            info = fetch_info(url, cfg_local)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502, detail=f"yt-dlp info failed: {exc}"
            )
        return YoutubeInfoModel(**info.to_dict())

    @app.post("/youtube/audio", response_model=TaskModel)
    def start_youtube_audio(payload: YouTubeAudioRequest) -> TaskModel:
        manager: TaskManager = app.state.tasks
        task = manager.enqueue_youtube_audio(payload.url)
        return TaskModel(**task.to_dict())

    @app.post("/youtube/video", response_model=TaskModel)
    def start_youtube_video(payload: YouTubeVideoRequest) -> TaskModel:
        manager: TaskManager = app.state.tasks
        task = manager.enqueue_youtube_video(payload.url, payload.height)
        return TaskModel(**task.to_dict())

    # -----------------------------------------------------------------------
    # Generic task polling
    # -----------------------------------------------------------------------

    @app.get("/tasks/{task_id}", response_model=TaskModel)
    def get_task(task_id: str) -> TaskModel:
        manager: TaskManager = app.state.tasks
        task = manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return TaskModel(**task.to_dict())

    @app.get("/tasks", response_model=List[TaskModel])
    def list_tasks(limit: int = Query(50, ge=1, le=200)) -> List[TaskModel]:
        manager: TaskManager = app.state.tasks
        return [TaskModel(**t.to_dict()) for t in manager.list_recent(limit=limit)]

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _search_hit_to_model(hit, cfg: Config) -> SearchHitModel:
    """Compute ``already_exported`` from filesystem, then project to the model."""
    stem = stem_from_metadata(hit.artists, hit.title)
    already = (cfg.export_dir / f"{stem}.m4a").is_file()
    return SearchHitModel(**hit.to_dict(), already_exported=already)
