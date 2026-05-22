# qwerf

A small toolkit to mirror your Yandex.Music **"My Favorites"** to a local
ALAC library that drops straight into Apple Music, with on-demand search &
download from a Chrome popup, **YouTube + YouTube Music + YouTube Music
playlists** support, and a queue + library-index that knows what you already
have.

The repo is a monorepo of three nested projects:

| Folder        | What it is                              | Runs as                               |
|---------------|------------------------------------------|----------------------------------------|
| `cli/`        | Sync engine + command-line tool          | `ymsync sync`, `ymsync yt audio …`     |
| `server/`     | Local listener for the Chrome extension  | `ymsync-server` (FastAPI, localhost)   |
| `extension/`  | Chrome extension popup (MV3)             | Loaded unpacked into Chrome            |

> **macOS only.** The default export folder is the system "Automatically Add
> to Music" inbox, and the conversion target is **ALAC m4a** so Apple Music
> imports everything natively. ffmpeg is required.

---

## Flow per track

```
        any source (Yandex / YouTube)
                       │
                       ▼
         Downloaded/<Artist - Title>.<ext>     (raw, kept as backup)
                       │
                  detect codec
                       │
            already ALAC ──► copy bytes ─┐
            something else ──► ffmpeg ───┤
                                          ▼
       ~/Music/Music/Media.localized/Automatically Add to Music.localized/
                <Artist - Title>.m4a
                       │
                       ▼
            Music.app picks it up
```

If **Auto-import is off** (toggle in the popup or `--no-auto-import-to-music`
during `init`), the pipeline stops after the Downloaded folder — nothing is
converted, nothing is copied.

YouTube videos go to a separate `videos_dir` with a per-resolution filename
tag (`<Channel - Title> [1080p].mp4`), so 720p and 1080p of the same video
can coexist.

## Library-aware dedup

A SQLite index (`~/.local/share/ymsync/library.db`) maps every audio file's
`(title, album, artists)` to its on-disk path + codec. Search hits, album
tracks, and YouTube playlist entries each carry an `already_exported` flag
the popup uses to render an "In library" badge **without** opening the file.
The server warm-scans your Downloaded + Exported folders on startup so the
cache is hot by the time you type into the search bar.

## Queue + rate limits

The listener has two pools:

* **Yandex.Music** — max 2 concurrent track downloads.
* **YouTube / YouTube Music** — max 3 concurrent (audio or video).

Beyond those caps everything queues. Closing the popup does **not** cancel
in-flight jobs; reopen and you'll see them in the "Active downloads" panel
with a per-row cancel button (or a "Cancel all").

## Quick start

```bash
# 1. Install ffmpeg
brew install ffmpeg

# 2. Install the CLI (pulls yt-dlp + yandex-music-* in)
cd cli && pip install -e .

# 3. Configure
ymsync init           # token, folders, auto-import on/off
ymsync index          # warm the SQLite library index from existing files

# 4. Sync favorites
ymsync sync

# 5. (Optional) start the listener for the Chrome extension
cd ../server && pip install -e .
ymsync-server
```

Then load `extension/` as an unpacked extension at `chrome://extensions`.

## Project layout

```
qwerf/
├── cli/               Sync engine + ymsync CLI (also exports the shared `ymsync` package)
├── server/            FastAPI listener consumed by the Chrome extension
├── extension/         Chrome MV3 popup
└── README.md
```
