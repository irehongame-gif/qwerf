# ymsync — CLI

Mirror your Yandex.Music **"My Favorites"** to a local lossless ALAC library.
Also ships a small `ymsync yt …` group that downloads audio/video from YouTube
(or any site yt-dlp supports) into the same folder layout, so the CLI and the
Chrome popup share one source of truth.

## Install

```bash
brew install ffmpeg
pip install -e .
```

The package depends on
[yandex-music-downloader](https://github.com/llistochek/yandex-music-downloader)
which is only published on GitHub; `pip install -e .` will pull it in. yt-dlp
comes from PyPI.

## Configure

```bash
ymsync init
```

This writes `~/.config/ymsync/config.toml`:

```toml
token        = "y0_..."             # Yandex OAuth token
download_dir = "~/Music/qwerf/Downloaded"
export_dir   = "~/Music/qwerf/Exported"
videos_dir   = "~/Movies/qwerf"     # YouTube video downloads land here
quality      = "lossless"           # lossless | normal | low (Yandex audio)
```

The token can also come from `YMSYNC_TOKEN` env var (overrides the file).

## Use

```bash
ymsync sync                 # sync all liked tracks (download missing, convert, export)
ymsync sync --limit 10      # only the 10 most recent likes
ymsync sync --dry-run       # show what would be done
ymsync download <track_id>  # one-off Yandex track by id or URL
ymsync where                # print download / export / videos folders

ymsync yt info  <url>                       # title / channel / available heights
ymsync yt audio <url>                       # download as AAC m4a → Exported/
ymsync yt video <url> --height 1080         # download as mp4 → videos_dir
```

## What `sync` does, exactly

For every liked track (newest first):

1. Compute a flat filename: `Artist - Title.m4a`.
2. If `Exported/Artist - Title.m4a` exists → **skip**.
3. Else if `Downloaded/Artist - Title.flac` exists → reuse it.
4. Else download lossless (FLAC) into `Downloaded/`.
5. Convert with `ffmpeg -c:a alac` into `Exported/`.

The original FLAC stays in `Downloaded/` as a backup.

## What `yt audio` / `yt video` do

Audio:

1. yt-dlp grabs the best audio stream and post-processes to AAC m4a in
   `Downloaded/Channel - Title.m4a`.
2. Copied to `Exported/Channel - Title.m4a` (no re-encode if already AAC,
   transcoded otherwise) — Apple Music plays it natively.

Video:

1. yt-dlp picks `bestvideo[height<=H]+bestaudio` and merges to mp4 at
   `videos_dir/Channel - Title [Hp].mp4`.
2. The height tag in the filename means downloading the same video at 720p
   and 1080p does not collide.

Both flows are idempotent: if the target already exists they're a no-op.

## Public API

The `ymsync` package is also imported by the listener server. Useful entry
points:

```python
from ymsync.config import load_config
from ymsync.client import build_client
from ymsync.pipeline import process_track, TrackResult, TrackStage
from ymsync.search import search_tracks
from ymsync.youtube import fetch_info, download_audio, download_video
```
