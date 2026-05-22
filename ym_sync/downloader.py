"""Track download and sync logic."""

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from yandex_music import Client, Track

from ym_sync.api import ApiTrackQuality, Container, CustomDownloadInfo, download_track as api_download_track, get_download_info

SAFE_FILENAME_RE = re.compile(r'[<>:"/\\|?*]')
FETCH_BATCH_SIZE = 50


@dataclass
class DownloadResult:
    """Result of downloading a track."""
    path: Path
    container: Container
    needs_conversion: bool


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


def build_filename(track: Track, track_id: str = None) -> str:
    """Build a sanitized filename from track metadata.

    Format: "Artist - Title [track_id]" (multiple artists joined by ", ").
    The track ID suffix prevents filename collisions when different tracks
    share the same artist/title combination.

    Args:
        track: Full Track object.
        track_id: Track ID to include as suffix. If None, uses track.id.

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

    tid = track_id if track_id is not None else str(track.id)
    filename = f"{artist_str} - {title} [{tid}]"
    return sanitize_filename(filename)


def is_track_in_state(track_id: str, state: Dict) -> bool:
    """Check if a track is already recorded in the sync state."""
    return str(track_id) in state


def _extension_for_container(container: Container) -> str:
    """Return the file extension for a given container type."""
    if container == Container.MP4:
        return ".m4a"
    elif container == Container.FLAC:
        return ".flac"
    elif container == Container.MP3:
        return ".mp3"
    return ".m4a"


def download_track(
    client: Client,
    track: Track,
    downloaded_dir: Path,
    delay: float = 0,
) -> DownloadResult:
    """Download a track in lossless quality and tag it.

    The API returns FLAC-in-MP4 (.m4a) when available, AAC-in-MP4 (.m4a)
    otherwise, or raw FLAC in rare cases. This function handles all
    container types and tags appropriately.

    Args:
        client: Initialized Yandex Music client.
        track: Full Track object to download.
        downloaded_dir: Directory to save the file.
        delay: Optional delay after download (seconds).

    Returns:
        DownloadResult with path, container type, and whether conversion is needed.
    """
    downloaded_dir.mkdir(parents=True, exist_ok=True)

    filename = build_filename(track)

    # Get download info for lossless quality
    download_info = get_download_info(track, ApiTrackQuality.LOSSLESS)
    container = download_info.file_format.container

    ext = _extension_for_container(container)
    file_path = downloaded_dir / f"{filename}{ext}"

    # Download and decrypt the track data
    track_data = api_download_track(client, download_info)

    # Write the file
    file_path.write_bytes(track_data)

    # Tag the file with metadata
    if container == Container.MP4:
        _tag_mp4(file_path, track)
    elif container == Container.FLAC:
        _tag_flac(file_path, track)
    # MP3 tagging not implemented (unlikely case)

    # Only raw FLAC or MP3 needs conversion to .m4a for export
    needs_conversion = container in (Container.FLAC, Container.MP3)

    if delay > 0:
        time.sleep(delay)

    return DownloadResult(
        path=file_path,
        container=container,
        needs_conversion=needs_conversion,
    )


def _tag_mp4(mp4_path: Path, track: Track) -> None:
    """Apply metadata tags to an MP4/M4A file.

    Args:
        mp4_path: Path to the .m4a file.
        track: Track object with metadata.
    """
    tag = MP4(str(mp4_path))

    title = track.title or ""
    tag["\xa9nam"] = title

    if track.artists:
        artist_names = [a.name for a in track.artists if a.name]
        tag["\xa9ART"] = "; ".join(artist_names)

    if track.albums:
        album = track.albums[0]
        if album.title:
            tag["\xa9alb"] = album.title
        if album.year:
            tag["\xa9day"] = str(album.year)
        if album.artists:
            album_artist_names = [a.name for a in album.artists if a.name]
            tag["aART"] = "; ".join(album_artist_names)
        if album.genre:
            tag["\xa9gen"] = album.genre
        if position := album.track_position:
            if position.index:
                tag["trkn"] = [(position.index, 0)]
            if position.volume:
                tag["disk"] = [(position.volume, 0)]

    tag.save()


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
