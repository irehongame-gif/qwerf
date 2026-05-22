"""Track download and sync logic."""

import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from mutagen.flac import FLAC
from yandex_music import Client, Track

from ym_sync.api import ApiTrackQuality, download_track, get_download_info

SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*]')
FETCH_BATCH_SIZE = 50


def fetch_favorites(client: Client) -> List[Track]:
    """Fetch all liked/favorite tracks for the authenticated user.

    Args:
        client: Initialized Yandex Music client.

    Returns:
        List of full Track objects.
    """
    likes = client.users_likes_tracks()
    if not likes:
        return []

    track_ids = []
    for short_track in likes:
        track_id = short_track.track_id
        if track_id:
            track_ids.append(track_id)

    if not track_ids:
        return []

    # Fetch full track info in batches
    all_tracks = []
    for i in range(0, len(track_ids), FETCH_BATCH_SIZE):
        batch = track_ids[i:i + FETCH_BATCH_SIZE]
        tracks = client.tracks(batch)
        if tracks:
            all_tracks.extend(tracks)

    return all_tracks


def sanitize_filename(name: str) -> str:
    """Remove or replace characters that are not safe for filenames."""
    sanitized = SAFE_FILENAME_RE.sub("_", name)
    # Remove leading/trailing whitespace and dots
    sanitized = sanitized.strip(" .")
    return sanitized


def build_filename(track: Track) -> str:
    """Build a sanitized filename from track metadata.

    Format: "Artist - Title" (multiple artists joined by ", ").

    Args:
        track: Full Track object.

    Returns:
        Sanitized filename string (without extension).
    """
    artists = []
    if track.artists:
        for artist in track.artists:
            if artist.name:
                artists.append(artist.name)

    artist_str = ", ".join(artists) if artists else "Unknown Artist"
    title = track.title or "Unknown Title"

    filename = f"{artist_str} - {title}"
    return sanitize_filename(filename)


def is_track_in_state(track_id: str, state: Dict) -> bool:
    """Check if a track is already recorded in the sync state."""
    return str(track_id) in state


def download_track_flac(
    client: Client,
    track: Track,
    downloaded_dir: Path,
    delay: float = 0,
) -> Path:
    """Download a track in FLAC/lossless quality and tag it.

    Args:
        client: Initialized Yandex Music client.
        track: Full Track object to download.
        downloaded_dir: Directory to save the FLAC file.
        delay: Optional delay after download (seconds).

    Returns:
        Path to the downloaded FLAC file.
    """
    downloaded_dir.mkdir(parents=True, exist_ok=True)

    filename = build_filename(track)
    flac_path = downloaded_dir / f"{filename}.flac"

    # Get download info for lossless quality
    download_info = get_download_info(track, ApiTrackQuality.LOSSLESS)

    # Download and decrypt the track data
    track_data = download_track(client, download_info)

    # Write the FLAC file
    flac_path.write_bytes(track_data)

    # Tag the file with metadata
    _tag_flac(flac_path, track)

    if delay > 0:
        time.sleep(delay)

    return flac_path


def _tag_flac(flac_path: Path, track: Track) -> None:
    """Apply metadata tags to a FLAC file.

    Args:
        flac_path: Path to the FLAC file.
        track: Track object with metadata.
    """
    audio = FLAC(str(flac_path))

    title = track.title or ""
    audio["title"] = title

    if track.artists:
        artist_names = [a.name for a in track.artists if a.name]
        audio["artist"] = artist_names

    if track.albums:
        album = track.albums[0]
        if album.title:
            audio["album"] = album.title
        if album.year:
            audio["date"] = str(album.year)
        if album.artists:
            album_artists = [a.name for a in album.artists if a.name]
            audio["albumartist"] = album_artists
        if position := album.track_position:
            if position.index:
                audio["tracknumber"] = str(position.index)

    audio.save()
