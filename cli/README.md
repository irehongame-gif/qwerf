# ymsync — CLI

Mirror your Yandex.Music **"My Favorites"** to a local ALAC library that
drops straight into Apple Music. Also exposes `ymsync yt …` for YouTube
audio/video downloads through the same pipeline.

## Install

```bash
brew install ffmpeg
pip install -e .
```

The package depends on
[yandex-music-downloader](https://github.com/llistochek/yandex-music-downloader)
which is only published on GitHub; `pip install -e .` will pull it in.
yt-dlp comes from PyPI.

## Configure

```bash
ymsync init
```

This writes `~/.config/ymsync/config.toml`:

```toml
token                = "y0_..."     # Yandex OAuth token
download_dir         = "~/Music/qwerf/Downloaded"
# Default points at the macOS Music.app auto-import inbox. Anything dropped
# here is imported into your Apple Music library on the next sync.
export_dir           = "~/Music/Music/Media.localized/Automatically Add to Music.localized"
videos_dir           = "~/Movies/qwerf"
db_path              = "~/.local/share/ymsync/library.db"
quality              = "lossless"   # lossless | normal | low (Yandex audio)
auto_import_to_music = true         # disable to keep raw Downloaded/ only
```

The token can also come from `YMSYNC_TOKEN` env var (overrides the file).

## Subcommands

```bash
ymsync init                                 # interactive setup
ymsync where                                # print resolved folders + flags
ymsync index [--rescan] [--parallel N]      # build / refresh the SQLite library index

ymsync sync [--limit N] [--dry-run]         # mirror "My Favorites" to the library
ymsync download <track-id-or-url>           # one-off Yandex track download

ymsync yt info <url>                        # title, channel, available video heights
ymsync yt playlist <url>                    # enumerate a playlist
ymsync yt audio <url>                       # download as ALAC m4a (or AAC if auto-import off)
ymsync yt video <url> [--height 1080]       # download as mp4
```

## Pipeline (what `sync`, `download`, `yt audio` do)

For every track, in order:

1. **Library index pre-flight** — the SQLite cache holds
   `(title, album, artists) → file path` for every audio file in your
   Downloaded + Exported folders. If this track's metadata already matches
   a row whose file is on disk, **skip**.
2. **Download** the lossless source into `Downloaded/`. Re-index that file.
3. If `auto_import_to_music` is **off**, stop here.
4. **Detect codec** of the downloaded file (mutagen first, ffprobe fallback).
5. **Convert to ALAC m4a** via ffmpeg unless the source is already ALAC,
   in which case we just copy the bytes (no re-encode). Cover art is
   preserved with `-c:v copy`.
6. The destination is the macOS **Automatically Add to Music** inbox; Music.app
   imports it automatically.
7. Re-index the ALAC copy.

YouTube video downloads bypass the ALAC pipeline and land in `videos_dir`
with a `[<height>p]` filename tag.

## Public Python API

The `ymsync` package is also imported by the listener server. Useful entry
points:

```python
from ymsync.config import load_config
from ymsync.client import build_client
from ymsync.audio_inspect import detect_codec, read_metadata
from ymsync.converter import to_alac
from ymsync.library_index import LibraryIndex, KIND_DOWNLOADED, KIND_EXPORTED
from ymsync.pipeline import process_track, TrackResult, TrackStage
from ymsync.search import search_tracks
from ymsync.yandex_album import get_album_with_tracks
from ymsync.youtube import fetch_info, fetch_playlist, download_audio, download_video
```
