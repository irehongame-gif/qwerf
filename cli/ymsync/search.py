"""Yandex.Music search — used by the listener server for the Chrome popup."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import List, Optional

from yandex_music import Client, Track


@dataclass
class SearchHit:
    """A single search result formatted for the Chrome extension."""

    id: str
    title: str
    artists: List[str]
    album: Optional[str]
    album_id: Optional[str]
    cover_url: Optional[str]
    duration_ms: int
    yandex_url: str
    available: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _cover_url(track: Track, size: str = "200x200") -> Optional[str]:
    if not track.cover_uri:
        return None
    return f"https://{track.cover_uri.replace('%%', size)}"


def _yandex_url(track: Track) -> str:
    album_id = None
    if track.albums:
        album_id = track.albums[0].id
    if album_id:
        return f"https://music.yandex.ru/album/{album_id}/track/{track.id}"
    return f"https://music.yandex.ru/track/{track.id}"


def hit_from_track(track: Track) -> SearchHit:
    return SearchHit(
        id=str(track.id),
        title=track.title or "",
        artists=[a.name for a in (track.artists or []) if a and a.name],
        album=(track.albums[0].title if track.albums else None),
        album_id=(str(track.albums[0].id) if track.albums else None),
        cover_url=_cover_url(track),
        duration_ms=int(track.duration_ms or 0),
        yandex_url=_yandex_url(track),
        available=bool(getattr(track, "available", True)),
    )


def search_tracks(client: Client, query: str, *, limit: int = 20) -> List[SearchHit]:
    """Search Yandex.Music for ``query`` and return formatted hits."""
    query = (query or "").strip()
    if not query:
        return []

    result = client.search(query, type_="track", nocorrect=False)
    if result is None or result.tracks is None:
        return []

    tracks = result.tracks.results or []
    return [hit_from_track(t) for t in tracks[:limit]]
