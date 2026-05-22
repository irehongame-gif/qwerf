"""Yandex.Music album enumeration.

Thin wrapper around ``client.albums_with_tracks(id)`` that gives us a flat
list of full :class:`yandex_music.Track` objects ready to feed into
:func:`ymsync.pipeline.process_track`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import List, Optional

from yandex_music import Client, Track


_ALBUM_URL_RE = re.compile(
    r"music\.yandex\.\w+/album/(?P<id>\d+)"
)


def extract_album_id(value: str) -> str:
    """Pull a numeric album id from a URL or numeric input."""
    if value.isdigit():
        return value
    if m := _ALBUM_URL_RE.search(value):
        return m.group("id")
    raise ValueError(
        f"Could not extract a Yandex album id from {value!r}; expected a "
        f"numeric id or a music.yandex.ru/album/... URL."
    )


@dataclass
class AlbumSummary:
    id: str
    title: str
    artists: List[str]
    cover_url: Optional[str]
    year: Optional[int]
    track_count: int
    yandex_url: str

    def to_dict(self) -> dict:
        return asdict(self)


def _cover_url(cover_uri: Optional[str], size: str = "200x200") -> Optional[str]:
    if not cover_uri:
        return None
    return f"https://{cover_uri.replace('%%', size)}"


def get_album_with_tracks(client: Client, album_id: str) -> tuple[AlbumSummary, List[Track]]:
    """Fetch album metadata + every track inside it."""
    album = client.albums_with_tracks(album_id)
    if album is None:
        raise LookupError(f"Yandex album not found: {album_id}")

    tracks: List[Track] = []
    for volume in (album.volumes or []):
        for tr in volume:
            if tr is not None:
                tracks.append(tr)

    artists = [a.name for a in (album.artists or []) if a and a.name]
    summary = AlbumSummary(
        id=str(album.id),
        title=(album.title or "").strip() or "Untitled album",
        artists=artists,
        cover_url=_cover_url(album.cover_uri, "300x300"),
        year=album.year,
        track_count=int(album.track_count or len(tracks)),
        yandex_url=f"https://music.yandex.ru/album/{album.id}",
    )
    return summary, tracks
