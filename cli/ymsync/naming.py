"""Filename helpers.

The library is laid out flat in ``Exported/`` (one file per track, no nested
folders) so we need stable, filesystem-safe filenames derived purely from the
track's artist + title.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from yandex_music import Track

# Characters disallowed by macOS / common filesystems. We're conservative so
# the same names work if the user later rsyncs to other machines.
_INVALID_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")

# macOS HFS+ / APFS allow 255 UTF-16 chars per filename component; we leave
# headroom for the suffix.
_MAX_STEM_LENGTH = 200


def _sanitize(part: str) -> str:
    part = _INVALID_CHARS_RE.sub("_", part)
    part = _WHITESPACE_RE.sub(" ", part).strip()
    # Avoid trailing dots/spaces which macOS / Windows dislike.
    part = part.rstrip(". ")
    return part or "Unknown"


def _join_artists(artists: Iterable[str]) -> str:
    names = [_sanitize(a) for a in artists if a]
    if not names:
        return "Unknown Artist"
    return ", ".join(names)


def track_stem(track: Track) -> str:
    """Return ``"Artist1, Artist2 - Title"`` (no suffix), filesystem safe."""
    artist_names = [a.name for a in (track.artists or []) if a and a.name]
    title = track.title or ""
    if version := getattr(track, "version", None):
        title = f"{title} ({version})"
    stem = f"{_join_artists(artist_names)} - {_sanitize(title)}"
    if len(stem) > _MAX_STEM_LENGTH:
        stem = stem[:_MAX_STEM_LENGTH].rstrip(". ")
    return stem


def downloaded_filename(track: Track, suffix: str = ".flac") -> str:
    return f"{track_stem(track)}{suffix}"


def exported_filename(track: Track) -> str:
    return f"{track_stem(track)}.m4a"



def stem_from_metadata(artists: Iterable[str], title: str, *, version: Optional[str] = None) -> str:
    """Filename stem from raw metadata (no Track object).

    Useful when we only have a search hit on hand and don't want a second
    network round-trip just to compute the same string.
    """
    full_title = f"{title} ({version})" if version else (title or "Unknown")
    stem = f"{_join_artists(artists)} - {_sanitize(full_title)}"
    if len(stem) > _MAX_STEM_LENGTH:
        stem = stem[:_MAX_STEM_LENGTH].rstrip(". ")
    return stem
