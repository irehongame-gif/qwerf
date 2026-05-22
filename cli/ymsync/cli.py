"""Command-line interface for ymsync.

Subcommands:
    init       – interactively write ~/.config/ymsync/config.toml
    sync       – mirror "My Favorites" to the local library
    download   – download a single track by id or URL
    where      – print resolved download/export folders + auto-import flag
    index      – build/refresh the SQLite library index
    yt         – yt-dlp powered helpers (audio / video / info / playlist)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

import click

from ymsync.client import build_client
from ymsync.config import (
    CONFIG_FILE,
    Config,
    ConfigError,
    DEFAULT_DOWNLOAD_DIR,
    DEFAULT_EXPORT_DIR,
    DEFAULT_VIDEOS_DIR,
    load_config,
    write_config,
)
from ymsync.converter import ensure_ffmpeg
from ymsync.library import fetch_track, list_liked_tracks
from ymsync.library_index import KIND_DOWNLOADED, KIND_EXPORTED, LibraryIndex
from ymsync.naming import track_stem
from ymsync.pipeline import TrackResult, TrackStage, process_track


_TRACK_URL_RE = re.compile(
    r"music\.yandex\.\w+/(?:album/\d+/)?track/(?P<id>\d+)"
)


def _extract_track_id(value: str) -> str:
    if value.isdigit():
        return value
    if m := _TRACK_URL_RE.search(value):
        return m.group("id")
    raise click.BadParameter(
        f"Could not extract a track id from {value!r}; pass a numeric id or a "
        "music.yandex.ru track URL."
    )


def _format_stage(result: TrackResult) -> str:
    icon = {
        TrackStage.PENDING: "·",
        TrackStage.QUEUED: "·",
        TrackStage.DOWNLOADING: "↓",
        TrackStage.CONVERTING: "→",
        TrackStage.EXPORTING: "↦",
        TrackStage.DONE: "✓",
        TrackStage.SKIPPED: "=",
        TrackStage.ERROR: "✗",
        TrackStage.CANCELLED: "⊘",
    }.get(result.stage, "·")
    label = result.stage.value
    return f"[{icon}] {label:<11} {result.stem}"


def _open_index(cfg: Config) -> LibraryIndex:
    """Open the on-disk library index. Cheap; does no scanning."""
    return LibraryIndex(cfg.db_path)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="ymsync")
def main() -> None:
    """Sync Yandex.Music \"My Favorites\" to a local lossless ALAC library."""


# ---------------------------------------------------------------------------
# init / where / index
# ---------------------------------------------------------------------------


@main.command()
@click.option("--token", prompt="Yandex.Music OAuth token", hide_input=True)
@click.option(
    "--download-dir",
    default=str(DEFAULT_DOWNLOAD_DIR),
    prompt="Downloaded folder (raw FLAC / yt-dlp output)",
    type=click.Path(),
)
@click.option(
    "--export-dir",
    default=str(DEFAULT_EXPORT_DIR),
    prompt="Exported folder (ALAC m4a, flat) — defaults to Music.app auto-import inbox",
    type=click.Path(),
)
@click.option(
    "--videos-dir",
    default=str(DEFAULT_VIDEOS_DIR),
    prompt="Videos folder (YouTube downloads)",
    type=click.Path(),
)
@click.option(
    "--quality",
    default="lossless",
    type=click.Choice(["lossless", "normal", "low"], case_sensitive=False),
    prompt="Yandex.Music audio quality",
)
@click.option(
    "--auto-import-to-music/--no-auto-import-to-music",
    default=True,
    prompt="Auto-import every download into Apple Music?",
)
def init(
    token: str,
    download_dir: str,
    export_dir: str,
    videos_dir: str,
    quality: str,
    auto_import_to_music: bool,
) -> None:
    """Write the config file (token + folders + quality + Music.app toggle)."""
    cfg = Config(
        token=token.strip(),
        download_dir=Path(download_dir),
        export_dir=Path(export_dir),
        videos_dir=Path(videos_dir),
        quality=quality.lower(),
        auto_import_to_music=auto_import_to_music,
    )
    cfg.ensure_dirs()
    path = write_config(cfg)
    try:
        ensure_ffmpeg(cfg.ffmpeg_path)
    except Exception as exc:  # noqa: BLE001
        click.secho(f"warning: {exc}", fg="yellow", err=True)
    click.secho(f"Wrote {path}", fg="green")


@main.command(name="where")
def where_cmd() -> None:
    """Print resolved download/export folders, db path, and auto-import flag."""
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"config:        {CONFIG_FILE}")
    click.echo(f"library db:    {cfg.db_path}")
    click.echo(f"downloaded:    {cfg.download_dir}")
    click.echo(f"exported:      {cfg.export_dir}")
    click.echo(f"videos:        {cfg.videos_dir}")
    click.echo(
        f"auto-import:   {'on' if cfg.auto_import_to_music else 'off'} "
        f"(Apple Music)"
    )


@main.command(name="index")
@click.option(
    "--rescan/--incremental",
    default=False,
    help="--rescan reads every file from scratch; default skips unchanged files.",
)
@click.option(
    "--parallel", type=int, default=4, show_default=True,
    help="Worker threads for tag-reading.",
)
def index_cmd(rescan: bool, parallel: int) -> None:
    """Build / refresh the SQLite library index from on-disk files.

    The server scans automatically on startup; you only need this command if
    you've manually moved files around and want to refresh without a restart.
    """
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    cfg.ensure_dirs()
    idx = _open_index(cfg)

    if rescan:
        # Drop everything; the next scan will repopulate.
        for row in idx.all_rows():
            idx.remove_file(row.path)

    targets = [(cfg.download_dir, KIND_DOWNLOADED)]
    if cfg.auto_import_to_music and cfg.export_dir.is_dir():
        targets.append((cfg.export_dir, KIND_EXPORTED))

    for directory, kind in targets:
        click.secho(f"Scanning {directory} as {kind}…", fg="cyan")
        result = idx.scan_directory(directory, kind, parallel=parallel)
        click.echo(
            f"  scanned={result.scanned} "
            f"new={result.inserted} "
            f"updated={result.updated} "
            f"unchanged={result.unchanged} "
            f"removed={result.removed}"
        )
        for err in result.errors[:10]:
            click.secho(f"  warning: {err}", fg="yellow", err=True)

    removed = idx.verify_existence()
    if removed:
        click.secho(f"Pruned {removed} stale row(s).", fg="cyan")
    click.secho("Done.", fg="green")


# ---------------------------------------------------------------------------
# sync / download
# ---------------------------------------------------------------------------


@main.command()
@click.option("--limit", type=int, default=None, help="Sync at most N most-recent likes.")
@click.option("--dry-run", is_flag=True, help="Show what would happen without downloading.")
def sync(limit: Optional[int], dry_run: bool) -> None:
    """Mirror \"My Favorites\" to the local library."""
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    cfg.ensure_dirs()
    if cfg.auto_import_to_music:
        ensure_ffmpeg(cfg.ffmpeg_path)
    idx = _open_index(cfg)

    client = build_client(cfg.token)
    click.secho("Fetching liked tracks…", fg="cyan")
    tracks = list_liked_tracks(client, limit=limit)
    click.secho(f"Found {len(tracks)} liked track(s).", fg="cyan")

    counts = {"done": 0, "skipped": 0, "error": 0, "cancelled": 0}

    for n, track in enumerate(tracks, 1):
        stem = track_stem(track)
        prefix = f"({n}/{len(tracks)})"
        if dry_run:
            click.echo(f"{prefix} would process  {stem}")
            continue

        def _progress(r: TrackResult, _prefix: str = prefix) -> None:
            click.echo(f"{_prefix} {_format_stage(r)}")

        result = process_track(
            track, cfg, library_index=idx, on_progress=_progress,
        )
        if result.stage == TrackStage.DONE:
            counts["done"] += 1
        elif result.stage == TrackStage.SKIPPED:
            counts["skipped"] += 1
        elif result.stage == TrackStage.CANCELLED:
            counts["cancelled"] += 1
        elif result.stage == TrackStage.ERROR:
            counts["error"] += 1
            click.secho(f"    error: {result.error}", fg="red", err=True)

    click.echo()
    click.secho(
        f"done: {counts['done']}  skipped: {counts['skipped']}  "
        f"errors: {counts['error']}",
        fg="green" if counts["error"] == 0 else "yellow",
    )
    if counts["error"]:
        sys.exit(1)


@main.command()
@click.argument("track")
def download(track: str) -> None:
    """Download a single track by numeric id or music.yandex.ru URL."""
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    cfg.ensure_dirs()
    if cfg.auto_import_to_music:
        ensure_ffmpeg(cfg.ffmpeg_path)
    idx = _open_index(cfg)

    track_id = _extract_track_id(track)
    client = build_client(cfg.token)
    full = fetch_track(client, track_id)

    def _progress(r: TrackResult) -> None:
        click.echo(_format_stage(r))

    result = process_track(full, cfg, library_index=idx, on_progress=_progress)
    if result.stage == TrackStage.ERROR:
        raise click.ClickException(result.error or "unknown error")
    if result.exported_path:
        click.secho(f"Exported: {result.exported_path}", fg="green")
    elif result.downloaded_path:
        click.secho(f"Downloaded: {result.downloaded_path}", fg="green")


# ---------------------------------------------------------------------------
# YouTube subcommands
# ---------------------------------------------------------------------------


@main.group("yt")
def yt_group() -> None:
    """YouTube audio / video downloads via yt-dlp."""


@yt_group.command("audio")
@click.argument("url")
def yt_audio(url: str) -> None:
    """Download YouTube audio as ALAC m4a (or AAC m4a if auto-import is off)."""
    from ymsync.youtube import download_audio

    try:
        cfg = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    cfg.ensure_dirs()
    if cfg.auto_import_to_music:
        ensure_ffmpeg(cfg.ffmpeg_path)
    idx = _open_index(cfg)

    def _progress(r: TrackResult) -> None:
        click.echo(_format_stage(r))

    result = download_audio(
        url, cfg, library_index=idx, on_progress=_progress,
    )
    if result.stage == TrackStage.ERROR:
        raise click.ClickException(result.error or "unknown error")
    if result.exported_path:
        click.secho(f"Exported: {result.exported_path}", fg="green")
    elif result.downloaded_path:
        click.secho(f"Downloaded: {result.downloaded_path}", fg="green")


@yt_group.command("video")
@click.argument("url")
@click.option(
    "--height",
    type=int,
    default=1080,
    show_default=True,
    help="Maximum video height (e.g. 720, 1080, 2160).",
)
def yt_video(url: str, height: int) -> None:
    """Download a YouTube video into the videos folder."""
    from ymsync.youtube import download_video

    try:
        cfg = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    cfg.ensure_dirs()
    ensure_ffmpeg(cfg.ffmpeg_path)

    def _progress(r: TrackResult) -> None:
        click.echo(_format_stage(r))

    result = download_video(url, height, cfg, on_progress=_progress)
    if result.stage == TrackStage.ERROR:
        raise click.ClickException(result.error or "unknown error")
    if result.exported_path:
        click.secho(f"Saved: {result.exported_path}", fg="green")


@yt_group.command("info")
@click.argument("url")
def yt_info(url: str) -> None:
    """Print metadata + available video heights for a YouTube URL."""
    from ymsync.youtube import fetch_info

    cfg = None
    try:
        cfg = load_config()
    except ConfigError:
        pass
    info = fetch_info(url, cfg)
    click.echo(f"title:    {info.title}")
    click.echo(f"channel:  {info.channel}")
    click.echo(f"duration: {info.duration_s}s")
    click.echo(
        f"heights:  {', '.join(f'{h}p' for h in info.available_heights) or '-'}"
    )
    if info.audio_already_exported:
        click.secho("audio:    already in library", fg="green")


@yt_group.command("playlist")
@click.argument("url")
def yt_playlist(url: str) -> None:
    """Print the contents of a YouTube / YouTube Music playlist."""
    from ymsync.youtube import fetch_playlist

    cfg = None
    idx = None
    try:
        cfg = load_config()
        idx = _open_index(cfg)
    except ConfigError:
        pass
    info = fetch_playlist(url, cfg, library_index=idx)
    click.echo(f"playlist: {info.title}")
    click.echo(f"uploader: {info.uploader or '-'}")
    click.echo(f"entries:  {info.entry_count}")
    click.echo()
    for n, e in enumerate(info.entries, 1):
        flag = "✓" if e.audio_already_exported else " "
        dur = f"{e.duration_s}s" if e.duration_s else " -"
        click.echo(f"{flag} {n:>3}. {e.title}  ({e.channel or '?'}, {dur})")


if __name__ == "__main__":
    main()
