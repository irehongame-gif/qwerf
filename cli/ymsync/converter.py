"""FLAC → ALAC (m4a) conversion via ffmpeg.

We re-encode audio to ALAC and copy the embedded cover stream as-is, so the
resulting m4a is fully tagged + arts the same as the source FLAC.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


class ConversionError(RuntimeError):
    pass


def ensure_ffmpeg(ffmpeg_path: str = "ffmpeg") -> None:
    """Raise :class:`ConversionError` if ffmpeg can't be found / executed."""
    if shutil.which(ffmpeg_path) is None:
        raise ConversionError(
            f"`{ffmpeg_path}` not found on PATH. On macOS: `brew install ffmpeg`."
        )


def flac_to_alac(
    src: Path,
    dst: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    overwrite: bool = False,
) -> Path:
    """Convert ``src`` FLAC to ``dst`` ALAC m4a. Returns ``dst``.

    The conversion is atomic: we write to ``dst.tmp.m4a`` first and rename on
    success, so an interrupted run can never leave a half-written export file
    that would later be mistaken for "already exported".
    """
    ensure_ffmpeg(ffmpeg_path)
    if not src.is_file():
        raise ConversionError(f"Source FLAC not found: {src}")
    if dst.exists() and not overwrite:
        return dst

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    if tmp.exists():
        tmp.unlink()

    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "error",
        "-y",                   # we control overwrite via tmp file naming
        "-i", str(src),
        "-map", "0",            # all streams (audio + cover)
        "-c:a", "alac",
        "-c:v", "copy",         # keep embedded cover art untouched
        "-movflags", "+faststart",
        str(tmp),
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ConversionError(f"Failed to launch ffmpeg: {exc}") from exc

    if proc.returncode != 0:
        if tmp.exists():
            tmp.unlink()
        raise ConversionError(
            f"ffmpeg failed (exit {proc.returncode}):\n{proc.stderr.strip()}"
        )

    tmp.replace(dst)
    return dst


def convert_if_needed(
    src: Path,
    dst: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
) -> Optional[Path]:
    """Convert ``src`` → ``dst`` only if ``dst`` does not yet exist."""
    if dst.exists():
        return None
    return flac_to_alac(src, dst, ffmpeg_path=ffmpeg_path)


def to_aac_m4a(
    src: Path,
    dst: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    bitrate: str = "256k",
    overwrite: bool = False,
) -> Path:
    """Transcode any audio source to AAC in an m4a container.

    Used for YouTube audio when yt-dlp returns opus/webm — Apple Music does
    not natively decode opus, so we transcode to AAC. We pick a generous
    bitrate (256k by default) to minimise generation loss.
    """
    ensure_ffmpeg(ffmpeg_path)
    if not src.is_file():
        raise ConversionError(f"Source audio not found: {src}")
    if dst.exists() and not overwrite:
        return dst

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    if tmp.exists():
        tmp.unlink()

    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(src),
        "-vn",                  # drop any video stream (e.g. webm w/ thumbnail)
        "-c:a", "aac",
        "-b:a", bitrate,
        "-movflags", "+faststart",
        str(tmp),
    ]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise ConversionError(f"Failed to launch ffmpeg: {exc}") from exc

    if proc.returncode != 0:
        if tmp.exists():
            tmp.unlink()
        raise ConversionError(
            f"ffmpeg failed (exit {proc.returncode}):\n{proc.stderr.strip()}"
        )

    tmp.replace(dst)
    return dst
