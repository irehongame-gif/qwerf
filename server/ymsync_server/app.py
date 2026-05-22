"""FastAPI app exposing search / download / status to the Chrome extension.

The app is bound to localhost only — it holds the Yandex token. CORS is open
for ``chrome-extension://*`` so the popup can call us via ``fetch``.

Routes:

==============  ========================================  ==================================
Method          Path                                      Purpose
==============  ========================================  ==================================
GET             /health                                   liveness check
GET             /settings                                 current runtime-tunable settings
PUT             /settings                                 update auto_import_to_music
GET             /search?q=…                               Yandex.Music search
GET             /tracks/{track_id}                        is this Yandex track exported?
GET             /yandex/track-info                        single-track context card
GET             /yandex/album-info                        album context (list of tracks)
POST            /yandex/album                             enqueue every track in an album
POST            /download                                 enqueue Yandex track download
GET             /youtube/info?url=…                       single-video context card
GET             /youtube/playlist-info?url=…              playlist context (list of entries)
POST            /youtube/audio                            enqueue YouTube audio
POST            /youtube/video                            enqueue YouTube video at <=H px
POST            /youtube/playlist                         enqueue every entry in a playlist
GET             /tasks/{task_id}                          poll any job
GET             /tasks?active=true                        list jobs (use active=true for popup re-attach)
POST            /tasks/{task_id}/cancel                   cancel a queued or running job
POST            /tasks/groups/{group_id}/cancel           cancel a whole album/playlist group
==============  ========================================  ==================================
"""

from __future__ import annotations

import re
import threading
from contextlib import asynccontextmanager
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ymsync.client import build_client
from ymsync.config import Config, load_config, update_settings
from ymsync.library import fetch_track
from ymsync.library_index import KIND_DOWNLOADED, KIND_EXPORTED, LibraryIndex
from ymsync.naming import stem_from_metadata
from ymsync.search import hit_from_track, search_tracks
from ymsync.yandex_album import extract_album_id, get_album_with_tracks

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


class YouTubeAudioRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")


class YouTubeVideoRequest(BaseModel):
    url: str = Field(..., description="YouTube video URL")
    height: int = Field(
        ..., ge=144, le=4320, description="Maximum video height (e.g. 1080)"
    )


class YouTubePlaylistRequest(BaseModel):
    url: str = Field(..., description="YouTube / YouTube Music playlist URL")
    mode: Literal["audio", "video"] = "audio"
    height: Optional[int] = Field(
        None, ge=144, le=4320,
        description="Required when mode='video'; per-video falls back automatically.",
    )


class YandexAlbumRequest(BaseModel):
    url: str = Field(..., description="music.yandex.ru/album/<id> URL or numeric id")


class YoutubeInfoModel(BaseModel):
    url: str
    title: str
    channel: str
    duration_s: int
    thumbnail_url: Optional[str] = None
    available_heights: List[int]
    audio_already_exported: bool = False
    expected_export_path: Optional[str] = None


class YoutubePlaylistEntryModel(BaseModel):
    url: str
    title: str
    channel: Optional[str] = None
    duration_s: Optional[int] = None
    thumbnail_url: Optional[str] = None
    audio_already_exported: bool = False


class YoutubePlaylistInfoModel(BaseModel):
    url: str
    title: str
    uploader: Optional[str] = None
    entry_count: int
    entries: List[YoutubePlaylistEntryModel]
    available_heights: List[int]


class AlbumSummaryModel(BaseModel):
    id: str
    title: str
    artists: List[str]
    cover_url: Optional[str] = None
    year: Optional[int] = None
    track_count: int
    yandex_url: str


class AlbumInfoModel(BaseModel):
    album: AlbumSummaryModel
    tracks: List[SearchHitModel]


class TaskModel(BaseModel):
    id: str
    kind: str
    dedup_key: str
    stage: str
    title: Optional[str] = None
    artists: List[str] = []
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
    cancellable: bool = True
    created_at: float
    updated_at: float


class GroupEnqueueModel(BaseModel):
    group_id: str
    group_label: str
    tasks: List[TaskModel]


class SettingsModel(BaseModel):
    auto_import_to_music: bool
    download_dir: str
    export_dir: str
    videos_dir: str
    db_path: str
    quality: str


class SettingsPatchModel(BaseModel):
    auto_import_to_music: Optional[bool] = None


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
        index = LibraryIndex(cfg.db_path)

        # Warm the index in the background so server startup isn't blocked
        # by a 10k-track scan on first run.
        threading.Thread(
            target=_initial_scan,
            args=(index, cfg),
            name="ymsync-initial-scan",
            daemon=True,
        ).start()

        manager = TaskManager(cfg, client, index)
        app.state.cfg = cfg
        app.state.client = client
        app.state.index = index
        app.state.tasks = manager
        try:
            yield
        finally:
            manager.shutdown()

    app = FastAPI(
        title="ymsync-server",
        version="0.3.0",
        description="Local listener for the qwerf Chrome extension.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^(chrome-extension://.*|http://(127\.0\.0\.1|localhost)(:\d+)?)$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["*"],
    )

    # -----------------------------------------------------------------------
    # Health + settings
    # -----------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": "0.3.0"}

    @app.get("/settings", response_model=SettingsModel)
    def get_settings() -> SettingsModel:
        cfg_local: Config = app.state.cfg
        return SettingsModel(
            auto_import_to_music=cfg_local.auto_import_to_music,
            download_dir=str(cfg_local.download_dir),
            export_dir=str(cfg_local.export_dir),
            videos_dir=str(cfg_local.videos_dir),
            db_path=str(cfg_local.db_path),
            quality=cfg_local.quality,
        )

    @app.put("/settings", response_model=SettingsModel)
    def put_settings(payload: SettingsPatchModel) -> SettingsModel:
        cfg_local: Config = app.state.cfg
        update_settings(cfg_local, auto_import_to_music=payload.auto_import_to_music)
        cfg_local.ensure_dirs()
        return get_settings()

    # -----------------------------------------------------------------------
    # Yandex.Music — search / track info / album / download
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
        return [_hit_to_model(h, cfg_local, app.state.index) for h in hits]

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
        return _hit_to_model(hit_from_track(track), app.state.cfg, app.state.index)

    @app.get("/yandex/album-info", response_model=AlbumInfoModel)
    def yandex_album_info(
        url: str = Query(..., description="music.yandex.ru/album/<id> URL or numeric id"),
    ) -> AlbumInfoModel:
        try:
            album_id = extract_album_id(url)
            summary, tracks = get_album_with_tracks(app.state.client, album_id)
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Yandex album lookup failed: {exc}")
        return AlbumInfoModel(
            album=AlbumSummaryModel(**summary.to_dict()),
            tracks=[
                _hit_to_model(hit_from_track(t), app.state.cfg, app.state.index)
                for t in tracks
            ],
        )

    @app.post("/download", response_model=TaskModel)
    def start_yandex_download(payload: StartDownloadRequest) -> TaskModel:
        manager: TaskManager = app.state.tasks
        task = manager.enqueue_yandex_track(payload.track_id)
        return TaskModel(**task.to_dict())

    @app.post("/yandex/album", response_model=GroupEnqueueModel)
    def enqueue_yandex_album(payload: YandexAlbumRequest) -> GroupEnqueueModel:
        manager: TaskManager = app.state.tasks
        try:
            group_id, group_label, tasks = manager.enqueue_yandex_album(payload.url)
        except (ValueError, LookupError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Yandex album enqueue failed: {exc}")
        return GroupEnqueueModel(
            group_id=group_id,
            group_label=group_label,
            tasks=[TaskModel(**t.to_dict()) for t in tasks],
        )

    # -----------------------------------------------------------------------
    # YouTube — info / playlist / audio / video
    # -----------------------------------------------------------------------

    @app.get("/youtube/info", response_model=YoutubeInfoModel)
    def youtube_info(
        url: str = Query(..., description="YouTube video URL"),
    ) -> YoutubeInfoModel:
        from ymsync.youtube import fetch_info  # lazy: heavy import

        cfg_local: Config = app.state.cfg
        try:
            info = fetch_info(url, cfg_local, library_index=app.state.index)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"yt-dlp info failed: {exc}")
        return YoutubeInfoModel(**info.to_dict())

    @app.get("/youtube/playlist-info", response_model=YoutubePlaylistInfoModel)
    def youtube_playlist_info(
        url: str = Query(..., description="YouTube / YouTube Music playlist URL"),
        limit: int = Query(200, ge=1, le=500),
    ) -> YoutubePlaylistInfoModel:
        from ymsync.youtube import fetch_playlist  # lazy: heavy import

        cfg_local: Config = app.state.cfg
        try:
            info = fetch_playlist(
                url, cfg_local, library_index=app.state.index, limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"yt-dlp playlist failed: {exc}")
        return YoutubePlaylistInfoModel(**info.to_dict())

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

    @app.post("/youtube/playlist", response_model=GroupEnqueueModel)
    def enqueue_youtube_playlist(payload: YouTubePlaylistRequest) -> GroupEnqueueModel:
        manager: TaskManager = app.state.tasks
        if payload.mode == "video" and payload.height is None:
            raise HTTPException(
                status_code=400,
                detail="`height` is required when mode='video'.",
            )
        try:
            group_id, group_label, tasks = manager.enqueue_youtube_playlist(
                payload.url, mode=payload.mode, height=payload.height,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"playlist enqueue failed: {exc}")
        return GroupEnqueueModel(
            group_id=group_id,
            group_label=group_label,
            tasks=[TaskModel(**t.to_dict()) for t in tasks],
        )

    # -----------------------------------------------------------------------
    # Generic task polling + cancellation
    # -----------------------------------------------------------------------

    @app.get("/tasks/{task_id}", response_model=TaskModel)
    def get_task(task_id: str) -> TaskModel:
        manager: TaskManager = app.state.tasks
        task = manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return TaskModel(**task.to_dict())

    @app.get("/tasks", response_model=List[TaskModel])
    def list_tasks(
        limit: int = Query(50, ge=1, le=200),
        active: bool = Query(False, description="If true, only non-terminal tasks."),
        group: Optional[str] = Query(None, description="Filter by group_id."),
    ) -> List[TaskModel]:
        manager: TaskManager = app.state.tasks
        if group is not None:
            tasks = manager.list_group(group)
            tasks.sort(key=lambda t: t.updated_at, reverse=True)
            tasks = tasks[:limit]
        else:
            tasks = manager.list_recent(limit=limit, active_only=active)
        return [TaskModel(**t.to_dict()) for t in tasks]

    @app.post("/tasks/{task_id}/cancel", response_model=TaskModel)
    def cancel_task(task_id: str) -> TaskModel:
        manager: TaskManager = app.state.tasks
        task = manager.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        manager.cancel(task_id)
        # Re-fetch — cancel() may have synchronously flipped the stage.
        task = manager.get(task_id)
        return TaskModel(**task.to_dict())  # type: ignore[union-attr]

    @app.post("/tasks/groups/{group_id}/cancel")
    def cancel_group(group_id: str) -> dict:
        manager: TaskManager = app.state.tasks
        n = manager.cancel_group(group_id)
        return {"group_id": group_id, "cancelled": n}

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hit_to_model(hit, cfg: Config, index: LibraryIndex) -> SearchHitModel:
    """Annotate a SearchHit with whether it's already in the library."""
    target_kinds = [KIND_EXPORTED] if cfg.auto_import_to_music else [KIND_DOWNLOADED]
    already = index.has_metadata(hit.title, hit.album, hit.artists, kinds=target_kinds)
    if not already and cfg.auto_import_to_music:
        # Filesystem fallback for files dropped into Exported by hand.
        stem = stem_from_metadata(hit.artists, hit.title)
        already = (cfg.export_dir / f"{stem}.m4a").is_file()
    return SearchHitModel(**hit.to_dict(), already_exported=already)


def _initial_scan(index: LibraryIndex, cfg: Config) -> None:
    """Background scan of the library so the index is warm by the time the
    user types into the popup search bar."""
    try:
        index.scan_directory(cfg.download_dir, KIND_DOWNLOADED, parallel=4)
        if cfg.auto_import_to_music and cfg.export_dir.is_dir():
            index.scan_directory(cfg.export_dir, KIND_EXPORTED, parallel=4)
    except Exception:
        # The cache is non-essential; log nothing and move on.
        pass
