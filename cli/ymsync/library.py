"""Yandex.Music library helpers — listing / hydrating liked tracks."""

from __future__ import annotations

from typing import Iterable, List, Sequence

from yandex_music import Client, Track


def list_liked_tracks(client: Client, *, limit: int | None = None) -> List[Track]:
    """Return full :class:`Track` objects for the user's "My Favorites".

    The Yandex API returns lightweight :class:`TrackShort` objects from
    ``users_likes_tracks``; we batch-hydrate them into full ``Track`` objects
    via ``client.tracks(ids)`` so we have access to artists / album / cover.
    """
    likes = client.users_likes_tracks()
    if likes is None or not likes.tracks:
        return []

    short_tracks = list(likes.tracks)
    if limit is not None:
        short_tracks = short_tracks[:limit]

    ids: list[str] = []
    for short in short_tracks:
        # `track_id` is "<id>:<albumId>" or just "<id>"; client.tracks accepts both.
        if (track_id := getattr(short, "track_id", None)):
            ids.append(track_id)
        elif (track_id := getattr(short, "id", None)):
            ids.append(str(track_id))

    if not ids:
        return []

    # client.tracks() accepts up to ~50 ids per call, but yandex-music handles
    # the batching internally.
    full = client.tracks(ids)
    # Filter out None / unavailable entries.
    return [t for t in full if t is not None]


def fetch_track(client: Client, track_id: str) -> Track:
    """Fetch a single full :class:`Track` by id (raises if not found)."""
    results = client.tracks([track_id])
    for t in results:
        if t is not None:
            return t
    raise LookupError(f"Track not found: {track_id}")


def chunk(iterable: Iterable, size: int) -> Iterable[Sequence]:
    """Yield successive ``size``-sized chunks from ``iterable``."""
    bucket: list = []
    for item in iterable:
        bucket.append(item)
        if len(bucket) == size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket
