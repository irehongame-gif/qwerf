"""Command-line interface for ymsync.

Subcommands:
    init       – interactively write ~/.config/ymsync/config.toml
    sync       – mirror "My Favorites" to the local library
    download   – download a single track by id or URL
    where      – print resolved download/export folders
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
        TrackStage.DOWNLOADING: "↓",
        TrackStage.CONVERTING: "→",
        TrackStage.DONE: "✓",
        TrackStage.SKIPPED: "=",
        TrackStage.ERROR: "✗",
    }[result.stage]
    label = result.stage.value
    return f"[{icon}] {label:<11} {result.stem}"


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="ymsync")
def main() -> None:
    """Sync Yandex.Music \"My Favorites\" to a local lossless ALAC library."""


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
    prompt="Exported folder (ALAC m4a, flat)",
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
def init(
    token: str,
    download_dir: str,
    export_dir: str,
    videos_dir: str,
    quality: str,
) -> None:
    """Write the config file (token + folders + quality)."""
    cfg = Config(
        token=token.strip(),
        download_dir=Path(download_dir),
        export_dir=Path(export_dir),
        videos_dir=Path(videos_dir),
        quality=quality.lower(),
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
    """Print resolved download/export folders and the config file path."""
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"config:     {CONFIG_FILE}")
    click.echo(f"downloaded: {cfg.download_dir}")
    click.echo(f"exported:   {cfg.export_dir}")
    click.echo(f"videos:     {cfg.videos_dir}")


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
    ensure_ffmpeg(cfg.ffmpeg_path)

    client = build_client(cfg.token)
    click.secho("Fetching liked tracks…", fg="cyan")
    tracks = list_liked_tracks(client, limit=limit)
    click.secho(f"Found {len(tracks)} liked track(s).", fg="cyan")

    counts = {"done": 0, "skipped": 0, "error": 0}

    for idx, track in enumerate(tracks, 1):
        stem = track_stem(track)
        prefix = f"({idx}/{len(tracks)})"
        if dry_run:
            click.echo(f"{prefix} would process  {stem}")
            continue

        def _progress(r: TrackResult, _prefix: str = prefix) -> None:
            # Live status line per track, overwritten as stage advances.
            click.echo(f"{_prefix} {_format_stage(r)}")

        result = process_track(track, cfg, on_progress=_progress)
        if result.stage == TrackStage.DONE:
            counts["done"] += 1
        elif result.stage == TrackStage.SKIPPED:
            counts["skipped"] += 1
        elif result.stage == TrackStage.ERROR:
            counts["error"] += 1
            click.secho(f"    error: {result.error}", fg="red", err=True)

    click.echo()
    click.secho(
        f"done: {counts['done']}  skipped: {counts['skipped']}  errors: {counts['error']}",
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
    ensure_ffmpeg(cfg.ffmpeg_path)

    track_id = _extract_track_id(track)
    client = build_client(cfg.token)
    full = fetch_track(client, track_id)

    def _progress(r: TrackResult) -> None:
        click.echo(_format_stage(r))

    result = process_track(full, cfg, on_progress=_progress)
    if result.stage == TrackStage.ERROR:
        raise click.ClickException(result.error or "unknown error")
    if result.exported_path:
        click.secho(f"Exported: {result.exported_path}", fg="green")


@main.group("yt")
def yt_group() -> None:
    """YouTube audio / video downloads via yt-dlp."""


@yt_group.command("audio")
@click.argument("url")
def yt_audio(url: str) -> None:
    """Download YouTube audio as AAC m4a into your library."""
    from ymsync.youtube import download_audio

    try:
        cfg = load_config()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    cfg.ensure_dirs()
    ensure_ffmpeg(cfg.ffmpeg_path)

    def _progress(r: TrackResult) -> None:
        click.echo(_format_stage(r))

    result = download_audio(url, cfg, on_progress=_progress)
    if result.stage == TrackStage.ERROR:
        raise click.ClickException(result.error or "unknown error")
    if result.exported_path:
        click.secho(f"Exported: {result.exported_path}", fg="green")


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

    try:
        cfg = load_config()
    except ConfigError:
        cfg = None
    info = fetch_info(url, cfg)
    click.echo(f"title:    {info.title}")
    click.echo(f"channel:  {info.channel}")
    click.echo(f"duration: {info.duration_s}s")
    click.echo(f"heights:  {', '.join(f'{h}p' for h in info.available_heights) or '-'}")
    if info.audio_already_exported:
        click.secho("audio:    already exported", fg="green")


if __name__ == "__main__":
    main()
