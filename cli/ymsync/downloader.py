"""Download a Yandex.Music track to a FLAC file (lossless by default).

Wraps :mod:`ymd.core` so we benefit from its tag/cover embedding while keeping
our own naming scheme — the upstream library writes nested
``Artist/Album/01 - Title.flac`` paths, we want flat ``Artist - Title.flac``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from yandex_music import Track

from ymd.core import (
    CoreTrackQuality,
    DownloadableTrack,
    download_track as ymd_download_track,
    to_downloadable_track,
)

from ymsync.naming import track_stem


_QUALITY_MAP = {
    "low": CoreTrackQuality.LOW,
    "normal": CoreTrackQuality.NORMAL,
    "lossless": CoreTrackQuality.LOSSLESS,
}


class DownloadError(RuntimeError):
    pass


def download_lossless(
    track: Track,
    download_dir: Path,
    *,
    quality: str = "lossless",
    embed_cover: bool = True,
) -> Path:
    """Download ``track`` into ``download_dir`` and return the resulting path.

    The filename is ``"{Artist} - {Title}.flac"`` (or ``.m4a`` for AAC, etc.).
    Tag-writing and cover embedding happens via the upstream downloader.
    """
    if quality not in _QUALITY_MAP:
        raise DownloadError(f"Unknown quality {quality!r}")
    download_dir.mkdir(parents=True, exist_ok=True)

    base_path = download_dir / track_stem(track)
    info: DownloadableTrack = to_downloadable_track(
        track=track,
        quality=_QUALITY_MAP[quality],
        base_path=base_path,
    )

    # If the file already exists at info.path with our chosen extension,
    # treat it as already downloaded (no network required).
    if info.path.exists():
        return info.path

    covers_cache: dict[int, object] = {}
    ymd_download_track(
        track_info=info,
        embed_cover=embed_cover,
        covers_cache=covers_cache,  # type: ignore[arg-type]
    )
    return info.path


def find_existing_download(track: Track, download_dir: Path) -> Optional[Path]:
    """Return an existing downloaded file for ``track`` if any (any audio ext)."""
    stem = track_stem(track)
    for ext in (".flac", ".m4a", ".mp3"):
        candidate = download_dir / f"{stem}{ext}"
        if candidate.is_file():
            return candidate
    return None
