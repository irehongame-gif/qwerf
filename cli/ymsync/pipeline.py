"""The end-to-end "process one track" pipeline.

This is the single source of truth used by both the CLI sync command and the
listener server endpoint, so that on-demand and bulk-sync behave identically.

Flow:

    1. Compute target export filename (``Artist - Title.m4a``).
    2. If exported file exists → done (skip).
    3. Else if downloaded file exists → reuse, skip download.
    4. Else download lossless FLAC into ``Downloaded/``.
    5. Convert FLAC → ALAC m4a into ``Exported/``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from yandex_music import Track

from ymsync.config import Config
from ymsync.converter import flac_to_alac
from ymsync.downloader import download_lossless, find_existing_download
from ymsync.naming import exported_filename, track_stem


class TrackStage(str, Enum):
    """Lifecycle stage of a single track. Used for progress reporting."""

    PENDING = "pending"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    DONE = "done"
    SKIPPED = "skipped"        # already in Exported/
    ERROR = "error"


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


def process_track(
    track: Track,
    cfg: Config,
    *,
    on_progress: Optional[ProgressCallback] = None,
) -> TrackResult:
    """Run the full download → convert → export flow for one track.

    The function is idempotent: calling it twice on a fully-exported track
    returns ``stage=SKIPPED`` and is a no-op.
    """
    cfg.ensure_dirs()
    stem = track_stem(track)
    result = TrackResult(track_id=str(track.id), stem=stem)

    def _emit(stage: TrackStage) -> None:
        result.stage = stage
        if on_progress:
            on_progress(result)

    export_path = cfg.export_dir / exported_filename(track)
    result.exported_path = export_path

    # Step 1 — already exported?
    if export_path.exists():
        _emit(TrackStage.SKIPPED)
        return result

    try:
        # Step 2 — already downloaded?
        existing = find_existing_download(track, cfg.download_dir)
        if existing is not None:
            result.downloaded_path = existing
        else:
            _emit(TrackStage.DOWNLOADING)
            result.downloaded_path = download_lossless(
                track,
                cfg.download_dir,
                quality=cfg.quality,
            )

        # Step 3 — convert if not FLAC, otherwise straight to ALAC.
        downloaded = result.downloaded_path
        if downloaded is None or not downloaded.is_file():
            raise RuntimeError("Download reported success but file is missing")

        _emit(TrackStage.CONVERTING)
        if downloaded.suffix.lower() == ".flac":
            flac_to_alac(downloaded, export_path, ffmpeg_path=cfg.ffmpeg_path)
        elif downloaded.suffix.lower() == ".m4a":
            # AAC/m4a path — upstream chose AAC quality; just copy into Exported.
            export_path.write_bytes(downloaded.read_bytes())
        else:
            # Fallback: re-encode anything else through ALAC as well.
            flac_to_alac(downloaded, export_path, ffmpeg_path=cfg.ffmpeg_path)

        _emit(TrackStage.DONE)
        return result
    except Exception as exc:  # noqa: BLE001 — we want to surface any failure
        result.stage = TrackStage.ERROR
        result.error = str(exc)
        if on_progress:
            on_progress(result)
        return result
