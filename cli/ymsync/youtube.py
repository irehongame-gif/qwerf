"""yt-dlp powered YouTube audio + video downloads.

A note on filename matching: video / audio titles routinely contain ``[…]``
(e.g. ``[Mash-Up]``, ``[Official Video]``) and our own height tag is
``[1080p]``. ``Path.glob`` interprets brackets as character classes, so
``Path('/x').glob('foo [1080p].*')`` does **not** match ``foo [1080p].mp4`` —
it matches ``foo 0.mp4``, ``foo 1.mp4`` … instead. We therefore avoid glob
entirely and use :func:`_find_produced` which compares filenames literally.

Same end-state as the Yandex pipeline:

* **Audio**: ``Downloaded/{Channel} - {Title}.m4a`` (raw, kept) →
  ``Exported/{Channel} - {Title}.m4a`` (flat, library-ready).
  YouTube has no lossless source, so we keep the AAC stream as-is in an m4a
  container — natively playable by Apple Music. We only re-encode if yt-dlp
  produced something else (opus/webm), and even then we transmux to AAC m4a.

* **Video**: ``videos_dir/{Channel} - {Title}.mp4`` — yt-dlp picks the best
  video stream up to the requested height and merges it with bestaudio.

Both flows are idempotent: if the target already exists they short-circuit to
``TrackStage.SKIPPED``.
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from ymsync.config import Config
from ymsync.naming import stem_from_metadata
from ymsync.pipeline import TrackResult, TrackStage


# Heights we let the user pick from in the popup. We filter this list by what
# yt-dlp actually reports for the URL so people aren't offered phantom 4K.
_HEIGHT_LADDER = (2160, 1440, 1080, 720, 480, 360, 240)


# ---------------------------------------------------------------------------
# /youtube/info — metadata only, no download
# ---------------------------------------------------------------------------


@dataclass
class YoutubeInfo:
    url: str
    title: str
    channel: str
    duration_s: int
    thumbnail_url: Optional[str]
    available_heights: List[int]
    expected_export_path: Optional[str] = None
    audio_already_exported: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def fetch_info(url: str, cfg: Optional[Config] = None) -> YoutubeInfo:
    """Pull title / channel / thumbnail / available heights from YouTube.

    If ``cfg`` is provided we also report whether the audio export already
    exists so the popup can show "In library" without a separate round-trip.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}

    title = (info.get("title") or "").strip() or "Untitled"
    channel = (
        info.get("uploader")
        or info.get("channel")
        or info.get("creator")
        or ""
    ).strip()
    duration_s = int(info.get("duration") or 0)
    thumbnail_url = _best_thumbnail(info)

    heights_present = {
        int(f["height"])
        for f in (info.get("formats") or [])
        if f.get("height")
    }
    max_h = max(heights_present, default=0)
    available_heights = [h for h in _HEIGHT_LADDER if h <= max_h]
    if not available_heights and heights_present:
        available_heights = sorted(heights_present, reverse=True)[:3]

    expected_export_path = None
    audio_already_exported = False
    if cfg is not None:
        stem = stem_from_metadata([channel] if channel else [], title)
        export_path = cfg.export_dir / f"{stem}.m4a"
        expected_export_path = str(export_path)
        audio_already_exported = export_path.is_file()

    return YoutubeInfo(
        url=url,
        title=title,
        channel=channel,
        duration_s=duration_s,
        thumbnail_url=thumbnail_url,
        available_heights=available_heights,
        expected_export_path=expected_export_path,
        audio_already_exported=audio_already_exported,
    )


def _find_produced(directory: Path, stem: str) -> Optional[Path]:
    """Return the first file in ``directory`` whose stem matches exactly.

    Avoids :meth:`pathlib.Path.glob` because the stems we deal with may
    contain ``[…]`` which glob would treat as a character class.
    """
    if not directory.is_dir():
        return None
    prefix = f"{stem}."
    for entry in directory.iterdir():
        if entry.is_file() and entry.name.startswith(prefix):
            return entry
    return None


def _best_thumbnail(info: dict) -> Optional[str]:
    if (single := info.get("thumbnail")):
        return single
    thumbs = info.get("thumbnails") or []
    if not thumbs:
        return None
    # Pick the largest reasonable one (yt-dlp orders by quality).
    return thumbs[-1].get("url")


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


def download_audio(
    url: str,
    cfg: Config,
    *,
    on_progress: Optional[Callable[[TrackResult], None]] = None,
) -> TrackResult:
    """yt-dlp the audio, transmux/transcode to AAC m4a, place in Exported/."""
    cfg.ensure_dirs()
    info = fetch_info(url)
    stem = stem_from_metadata([info.channel] if info.channel else [], info.title)

    result = TrackResult(track_id=url, stem=stem)
    export_path = cfg.export_dir / f"{stem}.m4a"
    result.exported_path = export_path
    result.extra["kind"] = "youtube_audio"
    result.extra["url"] = url

    def _emit(stage: TrackStage) -> None:
        result.stage = stage
        if on_progress:
            on_progress(result)

    if export_path.exists():
        _emit(TrackStage.SKIPPED)
        return result

    download_target = cfg.download_dir / f"{stem}.m4a"

    try:
        if not download_target.is_file():
            _emit(TrackStage.DOWNLOADING)
            _run_ytdlp_audio(url, cfg.download_dir, stem, on_progress, result)

            if not download_target.is_file():
                # yt-dlp may have produced a different extension if our
                # postprocessor was bypassed; locate whatever it dropped.
                fallback = _find_produced(cfg.download_dir, stem)
                if fallback is None:
                    raise RuntimeError(
                        "yt-dlp finished but no audio file was produced"
                    )
                download_target = fallback

        result.downloaded_path = download_target

        _emit(TrackStage.CONVERTING)
        if download_target.suffix.lower() == ".m4a":
            shutil.copyfile(download_target, export_path)
        else:
            from ymsync.converter import to_aac_m4a  # local import: optional dep

            to_aac_m4a(
                download_target,
                export_path,
                ffmpeg_path=cfg.ffmpeg_path,
            )

        _emit(TrackStage.DONE)
    except YtDlpDownloadError as exc:
        result.stage = TrackStage.ERROR
        result.error = f"yt-dlp: {exc}"
        if on_progress:
            on_progress(result)
    except Exception as exc:  # noqa: BLE001
        result.stage = TrackStage.ERROR
        result.error = str(exc)
        if on_progress:
            on_progress(result)

    return result


def _run_ytdlp_audio(
    url: str,
    download_dir: Path,
    stem: str,
    on_progress: Optional[Callable[[TrackResult], None]],
    result: TrackResult,
) -> None:
    out_template = str(download_dir / f"{stem}.%(ext)s")
    opts: dict = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "overwrites": False,
        # Postprocessor: strip into AAC/m4a regardless of source container.
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "0",  # let yt-dlp/ffmpeg pick best
            }
        ],
        "progress_hooks": [_make_progress_hook(on_progress, result)],
    }
    with YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------


def download_video(
    url: str,
    height: int,
    cfg: Config,
    *,
    on_progress: Optional[Callable[[TrackResult], None]] = None,
) -> TrackResult:
    """yt-dlp the best stream <= ``height`` px, merge to mp4 in videos_dir."""
    cfg.ensure_dirs()
    info = fetch_info(url)
    stem = stem_from_metadata([info.channel] if info.channel else [], info.title)
    tagged_stem = f"{stem} [{height}p]"

    result = TrackResult(track_id=f"{url}#{height}p", stem=tagged_stem)
    result.extra["kind"] = "youtube_video"
    result.extra["url"] = url
    result.extra["height"] = height

    # Existence check is per-(stem, height) so 720p and 1080p can coexist.
    existing = _find_produced(cfg.videos_dir, tagged_stem)
    if existing is not None:
        result.downloaded_path = existing
        result.exported_path = existing
        result.stage = TrackStage.SKIPPED
        if on_progress:
            on_progress(result)
        return result

    def _emit(stage: TrackStage) -> None:
        result.stage = stage
        if on_progress:
            on_progress(result)

    try:
        _emit(TrackStage.DOWNLOADING)
        out_template = str(cfg.videos_dir / f"{tagged_stem}.%(ext)s")
        opts: dict = {
            "format": (
                f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
            ),
            "outtmpl": out_template,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "overwrites": False,
            "progress_hooks": [_make_progress_hook(on_progress, result)],
        }
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        # ``merge_output_format='mp4'`` means the merged file is always at
        # ``<tagged_stem>.mp4``; check that first, then fall back to a literal
        # directory scan in case yt-dlp produced a different container (e.g.
        # mkv when an mp4-incompatible codec showed up).
        expected = cfg.videos_dir / f"{tagged_stem}.mp4"
        produced = expected if expected.is_file() else _find_produced(
            cfg.videos_dir, tagged_stem
        )
        if produced is None:
            raise RuntimeError(
                f"yt-dlp finished but no file named '{tagged_stem}.*' was "
                f"found in {cfg.videos_dir}"
            )
        result.downloaded_path = produced
        result.exported_path = produced
        _emit(TrackStage.DONE)
    except YtDlpDownloadError as exc:
        result.stage = TrackStage.ERROR
        result.error = f"yt-dlp: {exc}"
        if on_progress:
            on_progress(result)
    except Exception as exc:  # noqa: BLE001
        result.stage = TrackStage.ERROR
        result.error = str(exc)
        if on_progress:
            on_progress(result)

    return result


# ---------------------------------------------------------------------------
# Progress hook (shared by audio + video)
# ---------------------------------------------------------------------------


def _make_progress_hook(
    on_progress: Optional[Callable[[TrackResult], None]],
    result: TrackResult,
) -> Callable[[dict], None]:
    """yt-dlp callback that updates ``result.extra`` with byte-level progress.

    We don't want to emit a callback on every single byte tick (yt-dlp can
    fire hundreds per second), so we throttle by percent-buckets.
    """
    last_bucket = {"v": -1}

    def hook(payload: dict) -> None:
        if not on_progress:
            return
        status = payload.get("status")
        if status == "downloading":
            total = payload.get("total_bytes") or payload.get("total_bytes_estimate")
            done = payload.get("downloaded_bytes") or 0
            if total and total > 0:
                pct = int(done * 100 / total)
                bucket = pct // 5  # update every 5 %
                if bucket == last_bucket["v"]:
                    return
                last_bucket["v"] = bucket
                result.extra["progress_pct"] = pct
                result.extra["downloaded_bytes"] = done
                result.extra["total_bytes"] = total
            else:
                result.extra["downloaded_bytes"] = done
            on_progress(result)
        elif status == "finished":
            result.extra["progress_pct"] = 100
            on_progress(result)

    return hook
