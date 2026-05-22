# ymsync — CLI

Mirror your Yandex.Music **"My Favorites"** to a local lossless ALAC library.

## Install

```bash
brew install ffmpeg
pip install -e .
```

The package depends on
[yandex-music-downloader](https://github.com/llistochek/yandex-music-downloader)
which is only published on GitHub; `pip install -e .` will pull it in.

## Configure

```bash
ymsync init
```

This writes `~/.config/ymsync/config.toml`:

```toml
token        = "y0_..."         # Yandex OAuth token, see https://ym.marshal.dev/token/
download_dir = "~/Music/qwerf/Downloaded"
export_dir   = "~/Music/qwerf/Exported"
quality      = "lossless"       # lossless | normal | low
```

The token can also be supplied via the `YMSYNC_TOKEN` environment variable.

## Use

```bash
ymsync sync                 # sync all liked tracks (download missing, convert, export)
ymsync sync --limit 10      # only the 10 most recent likes
ymsync sync --dry-run       # show what would be done
ymsync download <track_id>  # one-off track download by Yandex track id
ymsync where                # print download / export folders
```

## What it does, exactly

For every liked track (newest first):

1. Compute a flat filename: `Artist - Title.m4a`
2. If `Exported/Artist - Title.m4a` already exists → **skip**
3. Else if `Downloaded/Artist - Title.flac` exists → reuse it
4. Else download it lossless (FLAC) into `Downloaded/`
5. Convert with `ffmpeg -c:a alac` into `Exported/` (flat, no nested folders)

The original FLAC stays in `Downloaded/` as a backup.

## Public API

The `ymsync` package is also imported by the listener server. The most useful
entry points are:

```python
from ymsync.config import load_config
from ymsync.client import build_client
from ymsync.pipeline import process_track, TrackResult
from ymsync.search import search_tracks
```
