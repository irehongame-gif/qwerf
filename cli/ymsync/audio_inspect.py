"""Detect the actual audio codec inside a file.

We never trust the file extension. ``.m4a`` could be ALAC or AAC; ``.mka``
could be FLAC or Opus; ``.wma.mp3`` is a thing some people apparently do.
This module gives us a single :func:`detect_codec` helper that returns a
canonical lowercase codec name, plus :func:`read_metadata` for the library
index.

Implementation: mutagen first (we already pull it in transitively through
``yandex-music-downloader``). For containers where mutagen can't tell us the
codec unambiguously (most often MP4/M4A) we fall back to ``ffprobe``.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import mutagen

# ---------------------------------------------------------------------------
# Canonical codec names — the rest of the codebase compares against these.
# ---------------------------------------------------------------------------

CODEC_ALAC = "alac"
CODEC_FLAC = "flac"
CODEC_AAC = "aac"
CODEC_MP3 = "mp3"
CODEC_OPUS = "opus"
CODEC_VORBIS = "vorbis"
CODEC_WAV = "pcm"
CODEC_UNKNOWN = "unknown"

# Codecs that should *never* be re-encoded to ALAC because they already are.
LOSSLESS_NATIVE = {CODEC_ALAC}


# ---------------------------------------------------------------------------
# Codec detection
# ---------------------------------------------------------------------------


def detect_codec(path: Path) -> str:
    """Return a canonical codec name for ``path``.

    Returns :data:`CODEC_UNKNOWN` if neither mutagen nor ffprobe could
    identify the format.
    """
    if not path.is_file():
        return CODEC_UNKNOWN
    name = _mutagen_codec(path)
    if name is not None:
        return name
    return _ffprobe_codec(path) or CODEC_UNKNOWN


def _mutagen_codec(path: Path) -> Optional[str]:
    try:
        f = mutagen.File(path)
    except Exception:
        return None
    if f is None:
        return None

    cls_name = type(f).__name__.lower()

    if "flac" in cls_name:
        return CODEC_FLAC
    if cls_name in {"mp3", "easymp3", "id3"}:
        return CODEC_MP3
    if "opus" in cls_name:
        return CODEC_OPUS
    if "vorbis" in cls_name or cls_name == "oggvorbis":
        return CODEC_VORBIS
    if "wave" in cls_name or cls_name == "wave":
        return CODEC_WAV

    if "mp4" in cls_name or path.suffix.lower() in {".m4a", ".m4b", ".mp4"}:
        info = getattr(f, "info", None) if f else None
        # mutagen.mp4 exposes both a short codec id ('mp4a', 'alac') and a
        # human-readable codec_description ('AAC LC' / 'Apple Lossless').
        codec = (getattr(info, "codec", None) or "").lower()
        desc = (getattr(info, "codec_description", None) or "").lower()
        if "alac" in codec or "alac" in desc or "apple lossless" in desc:
            return CODEC_ALAC
        if codec.startswith("mp4a") or "aac" in desc:
            return CODEC_AAC
        # fall through to ffprobe
        return None

    return None


def _ffprobe_codec(path: Path) -> Optional[str]:
    if shutil.which("ffprobe") is None:
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=nw=1:nk=1",
                str(path),
            ],
            check=False, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip().lower()
    if not raw:
        return None
    # ffprobe returns codec ids like 'alac', 'flac', 'aac', 'mp3', 'opus',
    # 'vorbis', 'pcm_s16le', etc.
    if raw == "alac":
        return CODEC_ALAC
    if raw == "flac":
        return CODEC_FLAC
    if raw == "aac":
        return CODEC_AAC
    if raw == "mp3":
        return CODEC_MP3
    if raw == "opus":
        return CODEC_OPUS
    if raw == "vorbis":
        return CODEC_VORBIS
    if raw.startswith("pcm"):
        return CODEC_WAV
    return raw  # propagate uncommon names verbatim


def needs_alac_conversion(codec: str) -> bool:
    """Return True if a file with this codec should go through ffmpeg.

    Rule: only ``alac`` is left alone (we just copy the bytes). Everything
    else — including AAC m4a from YouTube — gets re-encoded to ALAC so the
    user's library is uniformly Apple Lossless.
    """
    return codec.lower() not in LOSSLESS_NATIVE


# ---------------------------------------------------------------------------
# Tag reading for the library index
# ---------------------------------------------------------------------------


@dataclass
class TrackTags:
    title: Optional[str] = None
    album: Optional[str] = None
    artists: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.artists is None:
            self.artists = []


def read_metadata(path: Path) -> TrackTags:
    """Best-effort read of title / album / artists from any audio file.

    Returns an empty :class:`TrackTags` (all-None / empty) for files we can't
    parse — the library index treats those as "unknown" and falls back to
    filename matching.
    """
    if not path.is_file():
        return TrackTags()
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        return TrackTags()
    if f is None:
        return TrackTags()

    def _first(values) -> Optional[str]:
        if not values:
            return None
        if isinstance(values, str):
            return values.strip() or None
        try:
            return str(values[0]).strip() or None
        except (IndexError, TypeError):
            return None

    def _all(values) -> List[str]:
        if values is None:
            return []
        if isinstance(values, str):
            return [v.strip() for v in values.split(";") if v.strip()]
        try:
            return [str(v).strip() for v in values if str(v).strip()]
        except TypeError:
            return []

    # easy=True normalises to lowercase keys across containers.
    title = _first(f.get("title")) if hasattr(f, "get") else None
    album = _first(f.get("album")) if hasattr(f, "get") else None
    artists = _all(f.get("artist")) if hasattr(f, "get") else []
    if not artists:
        artists = _all(f.get("albumartist")) if hasattr(f, "get") else []

    return TrackTags(title=title, album=album, artists=artists)


# ---------------------------------------------------------------------------
# Normalisation helpers (used by the library index lookup)
# ---------------------------------------------------------------------------


def normalize(value: Optional[str]) -> str:
    """Lowercase + collapse-whitespace + strip-punct. Matching is forgiving.

    The pair-of-strings ``"Hey Jude (Remastered 2009)"`` and ``"Hey Jude"``
    won't match here because we don't strip parentheticals — that's
    deliberate, since "Hey Jude" and "Hey Jude (Live)" are different tracks.
    """
    if value is None:
        return ""
    return " ".join(value.lower().split())


def normalize_artists(artists: Iterable[str]) -> str:
    """Stable join for the artists column. Sorted so artist order doesn't
    cause spurious cache misses (collaborations are commutative as far as we
    care here)."""
    if not artists:
        return ""
    parts = sorted({normalize(a) for a in artists if a})
    return " | ".join(p for p in parts if p)
