"""Codec-aware audio conversion via ffmpeg.

The CLI is macOS-targeted, so the export pipeline always lands on
**ALAC m4a** (Apple Music's native lossless format). The single entry point
external code should care about is :func:`to_alac` — it inspects the actual
codec inside the source file (not the file extension) and either:

* copies the bytes verbatim when the source is already ALAC, or
* re-encodes to ALAC, copying any embedded cover art stream as-is.

About atomic writes
-------------------
We land on a sibling temp file first and rename on success, so a crashed
ffmpeg can never leave a half-written file the library index would mistake
for "already exported". Two small but important details:

* The temp filename keeps the **real** ``.m4a`` suffix and gets a leading
  ``.`` plus a ``qwerf-tmp`` token (e.g. ``.Foo.qwerf-tmp.m4a``). The
  leading dot hides it from Finder and from Music.app's auto-import scanner
  (Music.app skips dotfiles in its watch folder), and the preserved
  ``.m4a`` extension means ffmpeg doesn't have to guess the muxer.

* We *also* pass ``-f mp4`` explicitly to ffmpeg, so the output muxer is
  pinned regardless of filename. Belt + suspenders — if a future filename
  scheme ever hides the extension again, the conversion still works.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional

from ymsync.audio_inspect import (
    CODEC_ALAC,
    CODEC_UNKNOWN,
    detect_codec,
    needs_alac_conversion,
)


class ConversionError(RuntimeError):
    pass


def ensure_ffmpeg(ffmpeg_path: str = "ffmpeg") -> None:
    """Raise :class:`ConversionError` if ffmpeg can't be found / executed."""
    if shutil.which(ffmpeg_path) is None:
        raise ConversionError(
            f"`{ffmpeg_path}` not found on PATH. On macOS: `brew install ffmpeg`."
        )


# ---------------------------------------------------------------------------
# Universal ALAC conversion (the only thing new code should call)
# ---------------------------------------------------------------------------


def to_alac(
    src: Path,
    dst: Path,
    *,
    codec_hint: Optional[str] = None,
    ffmpeg_path: str = "ffmpeg",
    overwrite: bool = False,
) -> Path:
    """Produce an ALAC m4a file at ``dst`` from any audio source.

    * If ``src`` is already ALAC, just copies the bytes (no re-encode).
    * Otherwise re-encodes to ALAC. Any embedded cover stream is copied
      as-is (``-c:v copy``) so artwork survives the trip into Music.app.

    The write is atomic — we land on a hidden ``.<name>.qwerf-tmp.m4a``
    sibling first and rename on success, so a crashed run never leaves a
    half-written file that the library index would mistake for "already
    exported".

    Parameters
    ----------
    codec_hint:
        If you've already run :func:`audio_inspect.detect_codec` on ``src``,
        pass the result so we don't read the file twice.
    """
    if not src.is_file():
        raise ConversionError(f"Source audio not found: {src}")
    if dst.exists() and not overwrite:
        return dst

    codec = (codec_hint or detect_codec(src)).lower()

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path_for(dst)
    if tmp.exists():
        tmp.unlink()

    try:
        if not needs_alac_conversion(codec):
            # Already ALAC — just copy bytes. Skip ffmpeg entirely.
            shutil.copyfile(src, tmp)
        else:
            ensure_ffmpeg(ffmpeg_path)
            cmd = _build_alac_cmd(ffmpeg_path, src, tmp, codec)
            try:
                proc = subprocess.run(
                    cmd, check=False, capture_output=True, text=True
                )
            except OSError as exc:
                raise ConversionError(f"Failed to launch ffmpeg: {exc}") from exc
            if proc.returncode != 0:
                raise ConversionError(
                    f"ffmpeg failed (exit {proc.returncode}):\n{proc.stderr.strip()}"
                )
        tmp.replace(dst)
        return dst
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _tmp_path_for(dst: Path) -> Path:
    """Return a hidden sibling temp path that preserves ``dst``'s extension.

    Example: ``Foo.m4a`` → ``.Foo.qwerf-tmp.m4a``. The leading dot hides
    the file from Finder and from Music.app's auto-import scanner (which
    skips dotfiles in its watch folder), and the kept ``.m4a`` extension
    lets ffmpeg infer the muxer from the path.
    """
    stem, suffix = dst.stem, dst.suffix
    return dst.with_name(f".{stem}.qwerf-tmp{suffix}")


def _build_alac_cmd(
    ffmpeg_path: str, src: Path, tmp: Path, codec: str
) -> list[str]:
    """Build the ffmpeg command line for a given source codec."""
    base = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(src),
    ]

    # Cover-art handling differs by container:
    # * FLAC stores cover as a Vorbis-comment picture which ffmpeg exposes as
    #   a 'video' stream we can copy.
    # * Opus / Vorbis (in ogg / webm) often embed a JPEG that's compatible
    #   with the m4a 'covr' atom when copied.
    # * Lossy MP3 / AAC sources we re-encode into ALAC; copy the cover too.
    # * For unknown codecs just play it safe: only map audio.
    #
    # ``-f mp4`` pins the output muxer explicitly so we don't depend on
    # ffmpeg's filename-extension sniffing — see module docstring.
    if codec == CODEC_UNKNOWN:
        cmd = base + [
            "-map", "0:a",
            "-c:a", "alac",
            "-f", "mp4",
            "-movflags", "+faststart",
            str(tmp),
        ]
    else:
        cmd = base + [
            "-map", "0",
            "-c:a", "alac",
            "-c:v", "copy",
            "-f", "mp4",
            "-movflags", "+faststart",
            str(tmp),
        ]
    return cmd


# ---------------------------------------------------------------------------
# Backwards-compatible shims
# ---------------------------------------------------------------------------


def flac_to_alac(
    src: Path,
    dst: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    overwrite: bool = False,
) -> Path:
    """Legacy alias — prefer :func:`to_alac`."""
    return to_alac(
        src, dst,
        codec_hint="flac",
        ffmpeg_path=ffmpeg_path,
        overwrite=overwrite,
    )


def convert_if_needed(
    src: Path,
    dst: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
) -> Optional[Path]:
    """Convert ``src`` → ``dst`` only if ``dst`` does not yet exist."""
    if dst.exists():
        return None
    return to_alac(src, dst, ffmpeg_path=ffmpeg_path)


def to_aac_m4a(
    src: Path,
    dst: Path,
    *,
    ffmpeg_path: str = "ffmpeg",
    bitrate: str = "256k",
    overwrite: bool = False,
) -> Path:
    """Legacy: transcode to AAC m4a (used by the older YouTube-audio path).

    Kept for backward compatibility but the new pipeline always converts to
    ALAC instead. New code should not call this.
    """
    ensure_ffmpeg(ffmpeg_path)
    if not src.is_file():
        raise ConversionError(f"Source audio not found: {src}")
    if dst.exists() and not overwrite:
        return dst

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path_for(dst)
    if tmp.exists():
        tmp.unlink()

    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(src),
        "-vn",
        "-c:a", "aac",
        "-b:a", bitrate,
        "-f", "mp4",
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
