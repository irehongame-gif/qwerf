"""FastAPI app exposing search / download / status to the Chrome extension.

The app is bound to localhost only — it holds the Yandex token. CORS is open
for ``chrome-extension://*`` so the popup can call us via ``fetch``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from ymsync.client import build_client
from ymsync.config import Config, load_config
from ymsync.naming import exported_filename, stem_from_metadata
from ymsync.search import search_tracks

from ymsync_server.tasks import TaskManager


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


class TaskModel(BaseModel):
    id: str
    track_id: str
    stage: str
    title: Optional[str] = None
    artists: List[str] = []
    error: Optional[str] = None
    downloaded_path: Optional[str] = None
    exported_path: Optional[str] = None
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
        version="0.1.0",
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
    # Routes
    # -----------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": "0.1.0"}

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

        result: List[SearchHitModel] = []
        for hit in hits:
            stem = stem_from_metadata(hit.artists, hit.title)
            already = (cfg_local.export_dir / f"{stem}.m4a").is_file()
            result.append(
                SearchHitModel(
                    **hit.to_dict(),
                    already_exported=already,
                )
            )
        return result

    @app.get("/tracks/{track_id}", response_model=TrackStatusModel)
    def track_status(track_id: str) -> TrackStatusModel:
        manager: TaskManager = app.state.tasks
        path = manager.is_track_exported(track_id)
        return TrackStatusModel(
            track_id=track_id,
            exported=path is not None,
            exported_path=str(path) if path else None,
        )

    @app.post("/download", response_model=TaskModel)
    def start_download(payload: StartDownloadRequest) -> TaskModel:
        manager: TaskManager = app.state.tasks
        task = manager.enqueue(payload.track_id)
        return TaskModel(**task.to_dict())

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
