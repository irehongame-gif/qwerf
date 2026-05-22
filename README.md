# qwerf

A small toolkit to mirror your Yandex.Music **"My Favorites"** to a local
lossless ALAC (Apple-friendly) library, with on-demand search & download from
a Chrome popup, plus context-aware **YouTube audio + video** downloads when
you're on a YouTube tab.

The repo is a monorepo of three nested projects:

| Folder        | What it is                              | Runs as                               |
|---------------|------------------------------------------|----------------------------------------|
| `cli/`        | Sync engine + command-line tool          | `ymsync sync`, `ymsync yt audio …`     |
| `server/`     | Local listener for the Chrome extension  | `ymsync-server` (FastAPI, localhost)   |
| `extension/`  | Chrome extension popup (MV3)             | Loaded unpacked into Chrome            |

> Targeted at **macOS**. ffmpeg is required for FLAC → ALAC conversion and for
> yt-dlp's audio/video post-processing. yt-dlp is a Python dependency of the
> CLI package.

---

## Flow

For every Yandex.Music track (whether triggered by `sync` or by the popup):

```
Yandex.Music → Downloaded/<Artist - Title>.flac   (raw lossless, kept as backup)
              → ffmpeg -c:a alac
              → Exported/<Artist - Title>.m4a     (flat, no folders)
```

For YouTube audio:

```
YouTube → Downloaded/<Channel - Title>.m4a       (yt-dlp + AAC postproc)
        → Exported/<Channel - Title>.m4a         (copy / transmux to AAC m4a)
```

For YouTube video:

```
YouTube → videos_dir/<Channel - Title [<height>p]>.mp4
```

If the target already exists, the track/video is skipped entirely.

## Quick start

```bash
# 1. Install ffmpeg (macOS)
brew install ffmpeg

# 2. Install the CLI (pulls yt-dlp + yandex-music-* in)
cd cli
pip install -e .

# 3. Configure (token + folders, including the new videos folder)
ymsync init

# 4. Sync favorites
ymsync sync

# 5. (Optional) start the listener for the Chrome extension
cd ../server
pip install -e .
ymsync-server      # http://127.0.0.1:8765
```

Then load `extension/` as an unpacked extension at `chrome://extensions`.

## Getting a Yandex token

See <https://ym.marshal.dev/token/#implicit-oauth>. Drop the resulting token
into the config (or set `YMSYNC_TOKEN` env var).

## Project layout

```
qwerf/
├── cli/               Sync engine + ymsync CLI (also exports the shared `ymsync` package)
├── server/            FastAPI listener consumed by the Chrome extension
├── extension/         Chrome MV3 popup
└── README.md
```
