"""yt-dlp powered YouTube audio + video downloads.

A note on filename matching: video / audio titles routinely contain ``[…]``
(e.g. ``[Mash-Up]``, ``[Official Video]``) and our own height tag is
``[1080p]``. ``Path.glob`` interprets brackets as character classes, so
``Path('/x').glob('foo [1080p].*')`` does **not** match ``foo [1080p].mp4`` —
it matches ``foo 0.mp4``, ``foo 1.mp4`` … instead, and on titles like
``[Mash-Up]`` (reverse char-range ``h-U``) it actually raises. We therefore
avoid glob entirely and use :func:`_find_produced` which compares filenames
literally.

The audio path mirrors the Yandex pipeline:

  Downloaded/{Channel} - {Title}.m4a   (yt-dlp, AAC stream as-is)
        → codec detected (ALAC | AAC | OPUS | …)
        → if auto_import_to_music:
              ALAC m4a in Music.app inbox via converter.to_alac
        → library_index updated with whichever copies exist

The video path is the same as before: ``bestvideo[height<=H]+bestaudio``
merged to mp4 in ``videos_dir`` with a ``[Hp]`` filename tag.

Both flows are idempotent and respect a ``cancel_event``.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, List, Optional

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from ymsync.audio_inspect import detect_codec, read_metadata
from ymsync.config import Config
from ymsync.library_index import KIND_DOWNLOADED, KIND_EXPORTED, LibraryIndex
from ymsync.naming import stem_from_metadata
from ymsync.pipeline import TrackResult, TrackStage


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


def fetch_info(
    url: str,
    cfg: Optional[Config] = None,
    *,
    library_index: Optional[LibraryIndex] = None,
) -> YoutubeInfo:
    """Pull title / channel / thumbnail / available heights from YouTube.

    If ``cfg`` (and optionally ``library_index``) is provided we also report
    whether the audio is already in the user's library so the popup can show
    "In library" without a separate round-trip.
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
        audio_already_exported = _is_audio_in_library(
            cfg, library_index,
            title=title,
            artists=[channel] if channel else [],
            export_path=export_path,
        )

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


def _is_audio_in_library(
    cfg: Config,
    library_index: Optional[LibraryIndex],
    *,
    title: str,
    artists: List[str],
    export_path: Path,
) -> bool:
    """Decide whether the popup should show the audio button as 'In library'.

    Source of truth: the SQLite library index. We also fall back to a direct
    filesystem check on the expected export path so a user dropping files in
    by hand isn't ignored.
    """
    target_kinds = [KIND_EXPORTED] if cfg.auto_import_to_music else [KIND_DOWNLOADED]
    if library_index is not None:
        if library_index.has_metadata(title, None, artists, kinds=target_kinds):
            return True
    if cfg.auto_import_to_music and export_path.is_file():
        return True
    return False


def _find_produced(directory: Path, stem: str) -> Optional[Path]:
    """Return the first file in ``directory`` whose stem matches exactly."""
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
    return thumbs[-1].get("url")


# ---------------------------------------------------------------------------
# Cancellation plumbing
# ---------------------------------------------------------------------------


class _Cancelled(Exception):
    """Internal sentinel raised from a yt-dlp progress hook to abort."""


def _check_cancelled(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _Cancelled()


def _make_cancel_aware_progress_hook(
    on_progress: Optional[Callable[[TrackResult], None]],
    result: TrackResult,
    cancel_event: Optional[threading.Event],
) -> Callable[[dict], None]:
    """yt-dlp progress callback: throttled UI updates + cancel check."""
    last_bucket = {"v": -1}

    def hook(payload: dict) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled()  # yt-dlp will propagate, we'll catch outside
        if not on_progress:
            return
        status = payload.get("status")
        if status == "downloading":
            total = payload.get("total_bytes") or payload.get("total_bytes_estimate")
            done = payload.get("downloaded_bytes") or 0
            if total and total > 0:
                pct = int(done * 100 / total)
                bucket = pct // 5
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


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


def download_audio(
    url: str,
    cfg: Config,
    *,
    library_index: Optional[LibraryIndex] = None,
    on_progress: Optional[Callable[[TrackResult], None]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> TrackResult:
    """yt-dlp the audio → keep raw m4a → ALAC export to Music.app inbox.

    Honours ``cfg.auto_import_to_music`` (skips the Music.app step when off)
    and ``cancel_event`` (cancels mid-download via the yt-dlp progress hook).
    """
    cfg.ensure_dirs()
    info = fetch_info(url)
    stem = stem_from_metadata([info.channel] if info.channel else [], info.title)
    artists = [info.channel] if info.channel else []

    result = TrackResult(track_id=url, stem=stem)
    result.extra["kind"] = "youtube_audio"
    result.extra["url"] = url
    result.extra["title"] = info.title
    result.extra["artists"] = artists
    result.extra["channel"] = info.channel

    auto_import = cfg.auto_import_to_music
    target_kinds = [KIND_EXPORTED] if auto_import else [KIND_DOWNLOADED]

    def _emit(stage: TrackStage) -> None:
        result.stage = stage
        if on_progress:
            on_progress(result)

    # --- Pre-flight: library-index lookup ------------------------------
    if library_index is not None:
        rows = library_index.find_by_metadata(
            info.title, None, artists, kinds=target_kinds,
        )
        for row in rows:
            if Path(row.path).is_file():
                if row.kind == KIND_EXPORTED:
                    result.exported_path = Path(row.path)
                else:
                    result.downloaded_path = Path(row.path)
                _emit(TrackStage.SKIPPED)
                return result

    export_path = cfg.export_dir / f"{stem}.m4a"
    if auto_import:
        result.exported_path = export_path
        if export_path.exists():
            if library_index is not None:
                _index_safely(library_index, export_path, KIND_EXPORTED)
            _emit(TrackStage.SKIPPED)
            return result

    download_target = cfg.download_dir / f"{stem}.m4a"

    try:
        _check_cancelled(cancel_event)

        # --- Step 1: yt-dlp into Downloaded/ ---------------------------
        if not download_target.is_file():
            _emit(TrackStage.DOWNLOADING)
            _check_cancelled(cancel_event)
            _run_ytdlp_audio(
                url, cfg.download_dir, stem,
                on_progress=on_progress, result=result,
                cancel_event=cancel_event,
            )

            if not download_target.is_file():
                fallback = _find_produced(cfg.download_dir, stem)
                if fallback is None:
                    raise RuntimeError(
                        "yt-dlp finished but no audio file was produced"
                    )
                download_target = fallback

        result.downloaded_path = download_target
        if library_index is not None:
            _index_safely(library_index, download_target, KIND_DOWNLOADED)

        _check_cancelled(cancel_event)

        # --- Step 2: stop here if auto-import is off -------------------
        if not auto_import:
            result.exported_path = None
            _emit(TrackStage.DONE)
            return result

        # --- Step 3: codec analysis + ALAC conversion ------------------
        codec = detect_codec(download_target)
        result.extra["source_codec"] = codec
        _emit(TrackStage.CONVERTING)

        from ymsync.converter import to_alac

        to_alac(
            download_target,
            export_path,
            codec_hint=codec,
            ffmpeg_path=cfg.ffmpeg_path,
        )
        _check_cancelled(cancel_event)

        # --- Step 4: drop into Music.app inbox -------------------------
        _emit(TrackStage.EXPORTING)
        if library_index is not None:
            _index_safely(library_index, export_path, KIND_EXPORTED)

        _emit(TrackStage.DONE)
    except _Cancelled:
        result.stage = TrackStage.CANCELLED
        result.error = "cancelled by user"
        if on_progress:
            on_progress(result)
    except YtDlpDownloadError as exc:
        # yt-dlp wraps our _Cancelled into a DownloadError; unwrap it.
        if isinstance(getattr(exc, "exc_info", (None,))[1], _Cancelled):
            result.stage = TrackStage.CANCELLED
            result.error = "cancelled by user"
        else:
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
    *,
    on_progress: Optional[Callable[[TrackResult], None]],
    result: TrackResult,
    cancel_event: Optional[threading.Event],
) -> None:
    out_template = str(download_dir / f"{stem}.%(ext)s")
    opts: dict = {
        "format": "bestaudio[ext=m4a]/bestaudio",
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "overwrites": False,
        # Postprocessor: strip into AAC/m4a regardless of source container,
        # so the codec we see in `Downloaded/` is predictable.
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "0",
            }
        ],
        "progress_hooks": [
            _make_cancel_aware_progress_hook(on_progress, result, cancel_event)
        ],
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
    cancel_event: Optional[threading.Event] = None,
) -> TrackResult:
    """yt-dlp the best stream <= ``height`` px, merge to mp4 in videos_dir.

    yt-dlp's format string ``bestvideo[height<=H]+bestaudio`` already falls
    back to whatever's available — so passing height=1080 on a 480p-only
    video naturally lands a 480p file (just tagged as ``[1080p]`` in the
    filename, which is the height the user requested).
    """
    cfg.ensure_dirs()
    info = fetch_info(url)
    stem = stem_from_metadata([info.channel] if info.channel else [], info.title)
    tagged_stem = f"{stem} [{height}p]"

    result = TrackResult(track_id=f"{url}#{height}p", stem=tagged_stem)
    result.extra["kind"] = "youtube_video"
    result.extra["url"] = url
    result.extra["height"] = height
    result.extra["title"] = info.title
    result.extra["channel"] = info.channel

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
        _check_cancelled(cancel_event)
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
            "progress_hooks": [
                _make_cancel_aware_progress_hook(on_progress, result, cancel_event)
            ],
        }
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)

        # `merge_output_format='mp4'` means the final file is always at
        # `<tagged_stem>.mp4`; check that first, fall back to a literal
        # directory scan in case yt-dlp produced a different container.
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
    except _Cancelled:
        result.stage = TrackStage.CANCELLED
        result.error = "cancelled by user"
        if on_progress:
            on_progress(result)
    except YtDlpDownloadError as exc:
        if isinstance(getattr(exc, "exc_info", (None,))[1], _Cancelled):
            result.stage = TrackStage.CANCELLED
            result.error = "cancelled by user"
        else:
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
# Internal helpers
# ---------------------------------------------------------------------------


def _index_safely(idx: LibraryIndex, path: Path, kind: str) -> None:
    """Re-read tags from a file and upsert into the index. Never raises."""
    try:
        tags = read_metadata(path)
        idx.upsert_file(
            path,
            title=tags.title,
            album=tags.album,
            artists=tags.artists,
            kind=kind,
        )
    except Exception:
        # The index is a cache; never let an indexing failure surface.
        pass



# ---------------------------------------------------------------------------
# Playlist enumeration (YouTube + YouTube Music)
# ---------------------------------------------------------------------------


@dataclass
class PlaylistEntry:
    """One row in a playlist list-view."""

    url: str
    title: str
    channel: Optional[str]
    duration_s: Optional[int]
    thumbnail_url: Optional[str]
    audio_already_exported: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlaylistInfo:
    url: str
    title: str
    uploader: Optional[str]
    entry_count: int
    entries: List[PlaylistEntry]
    available_heights: List[int]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entries"] = [e.to_dict() if not isinstance(e, dict) else e for e in self.entries]
        return d


def fetch_playlist(
    url: str,
    cfg: Optional[Config] = None,
    *,
    library_index: Optional[LibraryIndex] = None,
    limit: int = 200,
) -> PlaylistInfo:
    """Enumerate a YouTube / YouTube Music playlist (no per-video downloads).

    Uses ``extract_flat="in_playlist"`` so we do *one* request to the playlist
    page rather than N+1 per-video round-trips. Each entry gets its title,
    uploader, duration and a thumbnail; full per-video format probing happens
    later, when the user actually clicks Download on a row.
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": int(limit),
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}

    if info.get("_type") != "playlist":
        # Single-video URL — wrap it in a 1-entry playlist for symmetry.
        single = info
        title = (single.get("title") or "").strip() or "Untitled"
        return PlaylistInfo(
            url=url,
            title=title,
            uploader=(single.get("uploader") or single.get("channel") or "").strip() or None,
            entry_count=1,
            entries=[
                PlaylistEntry(
                    url=single.get("webpage_url") or url,
                    title=title,
                    channel=(single.get("uploader") or single.get("channel") or "").strip() or None,
                    duration_s=int(single.get("duration") or 0) or None,
                    thumbnail_url=_best_thumbnail(single),
                )
            ],
            available_heights=[],
        )

    entries: List[PlaylistEntry] = []
    raw_entries = info.get("entries") or []
    for entry in raw_entries:
        if entry is None:
            continue
        entry_url = (
            entry.get("webpage_url")
            or entry.get("url")
            or _entry_to_watch_url(entry)
        )
        if not entry_url:
            continue
        title = (entry.get("title") or "").strip() or "Untitled"
        channel = (entry.get("uploader") or entry.get("channel") or "").strip() or None
        duration_raw = entry.get("duration")
        duration_s = int(duration_raw) if duration_raw is not None else None
        thumb = _best_thumbnail(entry)

        already_exported = False
        if cfg is not None and channel:
            already_exported = _is_audio_in_library(
                cfg, library_index,
                title=title,
                artists=[channel],
                export_path=cfg.export_dir / f"{stem_from_metadata([channel], title)}.m4a",
            )

        entries.append(
            PlaylistEntry(
                url=entry_url,
                title=title,
                channel=channel,
                duration_s=duration_s,
                thumbnail_url=thumb,
                audio_already_exported=already_exported,
            )
        )

    return PlaylistInfo(
        url=url,
        title=(info.get("title") or "").strip() or "Untitled playlist",
        uploader=(info.get("uploader") or info.get("channel") or "").strip() or None,
        entry_count=int(info.get("playlist_count") or len(entries)),
        entries=entries,
        # We could probe one entry to learn the height ladder, but that's a
        # second network round-trip. The popup defaults to 1080p and yt-dlp
        # falls back per-video at download time anyway.
        available_heights=list(_HEIGHT_LADDER[2:]),  # 1080..240
    )


def _entry_to_watch_url(entry: dict) -> Optional[str]:
    """Best-effort reconstruction of a watch URL from a flat playlist entry."""
    if (vid := entry.get("id")):
        if entry.get("ie_key", "").lower() in {"youtube", "youtubemusic", ""}:
            return f"https://www.youtube.com/watch?v={vid}"
    return None
