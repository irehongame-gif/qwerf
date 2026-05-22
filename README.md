# qwerf

A small toolkit to mirror your Yandex.Music **"My Favorites"** to a local lossless
ALAC (Apple-friendly) library, plus on-demand search & download from a Chrome popup.

The repo is a monorepo of three nested projects:

| Folder        | What it is                              | Runs as                               |
|---------------|------------------------------------------|----------------------------------------|
| `cli/`        | Sync engine + command-line tool          | `ymsync sync`                          |
| `server/`     | Local listener for the Chrome extension  | `ymsync-server` (FastAPI, localhost)   |
| `extension/`  | Chrome extension popup (MV3)             | Loaded unpacked into Chrome            |

> Targeted at **macOS**. ffmpeg is required for FLAC → ALAC conversion.

---

## Flow

For every track (whether triggered by `sync` or by the extension):

```
Yandex.Music → Downloaded/<Artist - Title>.flac   (raw lossless, kept as backup)
              → ffmpeg -c:a alac
              → Exported/<Artist - Title>.m4a     (flat, no folders)
```

If the file already exists in `Exported/`, the track is skipped entirely.
If it exists in `Downloaded/` but not in `Exported/`, only the convert step runs.

## Quick start

```bash
# 1. Install ffmpeg (macOS)
brew install ffmpeg

# 2. Install the CLI
cd cli
pip install -e .

# 3. Configure (token + folders)
ymsync init        # writes ~/.config/ymsync/config.toml interactively

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
├── cli/               Sync engine (also exposes the shared `ymsync` package)
├── server/            FastAPI listener consumed by the Chrome extension
├── extension/         Chrome MV3 popup
└── README.md
```
