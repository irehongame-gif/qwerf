"""The end-to-end "process one track" pipeline.

This is the single source of truth used by both the CLI sync command and the
listener server endpoint, so on-demand and bulk-sync behave identically.

Flow:

    1. Pre-flight skip check via :class:`LibraryIndex`. If we already have
       an exported (or, when auto-import is off, downloaded) copy with the
       same ``(title, artists)`` we short-circuit to ``SKIPPED``.
    2. Otherwise: download a lossless FLAC into ``download_dir`` and upsert
       the file into the index.
    3. If ``auto_import_to_music`` is enabled, run :func:`converter.to_alac`
       (which copies if already ALAC, else re-encodes) into the macOS
       Music.app auto-import folder, and upsert that copy into the index too.

Stages exposed to the UI:

    PENDING → QUEUED → DOWNLOADING → CONVERTING → EXPORTING → DONE
                                                            ↘ SKIPPED
                                                            ↘ ERROR
                                                            ↘ CANCELLED
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from yandex_music import Track

from ymsync.audio_inspect import detect_codec, read_metadata
from ymsync.config import Config
from ymsync.converter import to_alac
from ymsync.downloader import download_lossless, find_existing_download
from ymsync.library_index import KIND_DOWNLOADED, KIND_EXPORTED, LibraryIndex
from ymsync.naming import exported_filename, track_stem


class TrackStage(str, Enum):
    """Lifecycle stage of a single track. Used for progress reporting."""

    PENDING = "pending"
    QUEUED = "queued"            # waiting for a worker slot
    DOWNLOADING = "downloading"
    CONVERTING = "converting"    # ffmpeg / codec analysis
    EXPORTING = "exporting"      # copying to Music.app inbox
    DONE = "done"
    SKIPPED = "skipped"          # already in the library
    ERROR = "error"
    CANCELLED = "cancelled"      # user pressed stop


# Stages that mean "the job is no longer doing useful work".
_TERMINAL = {TrackStage.DONE, TrackStage.SKIPPED, TrackStage.ERROR, TrackStage.CANCELLED}


def is_terminal(stage: TrackStage) -> bool:
    return stage in _TERMINAL


@dataclass
class TrackResult:
    track_id: str
    stem: str
    stage: TrackStage = TrackStage.PENDING
    downloaded_path: Optional[Path] = None
    exported_path: Optional[Path] = None
    error: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.stage in (TrackStage.DONE, TrackStage.SKIPPED)


ProgressCallback = Callable[[TrackResult], None]


class _Cancelled(Exception):
    """Internal sentinel — pipeline saw cancel_event and is bailing out."""


def _check_cancelled(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _Cancelled()


# ---------------------------------------------------------------------------
# Track metadata helpers
# ---------------------------------------------------------------------------


def _track_metadata(track: Track) -> tuple[Optional[str], Optional[str], list[str]]:
    """Pull (title, album, artists) out of a Yandex Track for index lookup."""
    title = (track.title or "").strip() or None
    album = None
    if track.albums:
        album = (track.albums[0].title or "").strip() or None
    artists = [a.name for a in (track.artists or []) if a and a.name]
    return title, album, artists


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------


def process_track(
    track: Track,
    cfg: Config,
    *,
    library_index: Optional[LibraryIndex] = None,
    on_progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
) -> TrackResult:
    """Run download → (convert → export) → index for one track.

    The function is idempotent: calling it twice on a fully-exported track
    returns ``stage=SKIPPED`` and is a no-op.
    """
    cfg.ensure_dirs()
    stem = track_stem(track)
    result = TrackResult(track_id=str(track.id), stem=stem)

    title, album, artists = _track_metadata(track)
    result.extra["title"] = title
    result.extra["album"] = album
    result.extra["artists"] = artists

    auto_import = cfg.auto_import_to_music
    target_kinds = [KIND_EXPORTED] if auto_import else [KIND_DOWNLOADED]

    def _emit(stage: TrackStage) -> None:
        result.stage = stage
        if on_progress:
            on_progress(result)

    # --- Step 0: index pre-flight ----------------------------------------
    # Using the SQLite cache lets us skip without ever touching the disk
    # (or hitting the network), which makes "Download All" on a 50-track
    # album fast.
    if library_index is not None:
        existing_rows = library_index.find_by_metadata(
            title, album, artists, kinds=target_kinds,
        )
        for row in existing_rows:
            if Path(row.path).is_file():
                if row.kind == KIND_EXPORTED:
                    result.exported_path = Path(row.path)
                else:
                    result.downloaded_path = Path(row.path)
                _emit(TrackStage.SKIPPED)
                return result

    # --- Step 1: filesystem fall-back skip check (auto-import case) ------
    # If the file lives in Exported under our naming scheme but the index
    # hasn't seen it yet (e.g. user just dropped a backup in), accept it.
    if auto_import:
        export_path = cfg.export_dir / exported_filename(track)
        result.exported_path = export_path
        if export_path.exists():
            if library_index is not None:
                _index_existing(library_index, export_path, KIND_EXPORTED)
            _emit(TrackStage.SKIPPED)
            return result

    try:
        _check_cancelled(cancel_event)

        # --- Step 2: download (or reuse) raw FLAC ------------------------
        existing = find_existing_download(track, cfg.download_dir)
        if existing is not None:
            result.downloaded_path = existing
        else:
            _emit(TrackStage.DOWNLOADING)
            _check_cancelled(cancel_event)
            result.downloaded_path = download_lossless(
                track, cfg.download_dir, quality=cfg.quality,
            )

        downloaded = result.downloaded_path
        if downloaded is None or not downloaded.is_file():
            raise RuntimeError("Download reported success but file is missing")

        # Always (re)index the downloaded copy — it might be new.
        if library_index is not None:
            _index_existing(library_index, downloaded, KIND_DOWNLOADED)

        _check_cancelled(cancel_event)

        # --- Step 3: stop here if auto-import is off ---------------------
        if not auto_import:
            result.exported_path = None
            _emit(TrackStage.DONE)
            return result

        # --- Step 4: codec analysis + ALAC conversion --------------------
        codec = detect_codec(downloaded)
        result.extra["source_codec"] = codec
        _emit(TrackStage.CONVERTING)
        export_path = cfg.export_dir / exported_filename(track)
        result.exported_path = export_path
        to_alac(
            downloaded,
            export_path,
            codec_hint=codec,
            ffmpeg_path=cfg.ffmpeg_path,
        )
        _check_cancelled(cancel_event)

        # --- Step 5: drop into Music.app inbox ---------------------------
        # `export_path` already lives in the auto-import folder; "exporting"
        # is here just so the UI can show the final stage transition.
        _emit(TrackStage.EXPORTING)
        if library_index is not None:
            _index_existing(library_index, export_path, KIND_EXPORTED)

        _emit(TrackStage.DONE)
        return result

    except _Cancelled:
        result.stage = TrackStage.CANCELLED
        result.error = "cancelled by user"
        if on_progress:
            on_progress(result)
        return result
    except Exception as exc:  # noqa: BLE001 — any failure is reported
        result.stage = TrackStage.ERROR
        result.error = str(exc)
        if on_progress:
            on_progress(result)
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _index_existing(idx: LibraryIndex, path: Path, kind: str) -> None:
    """Re-read tags + codec for ``path`` and upsert into the index.

    We use the file's actual tags (rather than the Yandex Track values) so
    library searches that originate from non-Yandex sources — a manual
    drop-in, a YouTube audio export, anything tagged with mutagen — all hit
    the same row.
    """
    try:
        tags = read_metadata(path)
    except Exception:
        tags = None
    try:
        idx.upsert_file(
            path,
            title=(tags.title if tags else None),
            album=(tags.album if tags else None),
            artists=(tags.artists if tags else None),
            kind=kind,
        )
    except Exception:
        # The index is a cache, never the source of truth — never fail the
        # user-visible pipeline because of an index write hiccup.
        pass
