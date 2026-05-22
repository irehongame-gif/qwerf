# qwerf — Chrome extension

Search Yandex.Music from a Chrome popup, **plus**:

* One-click download when the active tab is a `music.yandex.ru/track/…` page.
* Whole-album download when the active tab is a `music.yandex.ru/album/<id>` page.
* Per-video audio + per-resolution video download for any
  `youtube.com/watch?v=…` or `youtu.be/…` page.
* Whole-playlist download (audio or video) for `youtube.com/playlist?list=…`,
  `music.youtube.com/...&list=…`, or any `?list=` watch URL with a real
  playlist id (auto-radios `RD…` are demoted to single-video).

Plus an **Auto-import** toggle in the topbar (mirrors the server's
`auto_import_to_music` flag) and an **Active downloads** panel that survives
popup closes — open the popup again and you see what's still running.

## Load it

1. Run the listener once on your Mac: `ymsync-server`.
2. Open `chrome://extensions`, enable **Developer mode**, click **Load unpacked**
   and pick this `extension/` folder.
3. Pin the qwerf icon to your toolbar — the popup is the whole UI.

## Settings

* **Auto-import toggle** (topbar pill) — flips the server-side
  `auto_import_to_music` flag via `PUT /settings`. When off, downloads stop
  after the Downloaded folder; nothing is converted, nothing is copied into
  Apple Music.
* **Server URL** (gear icon → settings panel) — defaults to
  `http://127.0.0.1:8765`, stored per-Chrome-profile via `chrome.storage.sync`.

## What you'll see

| Active tab                                  | Popup shows                                                         |
|----------------------------------------------|----------------------------------------------------------------------|
| Anywhere else                                | Search bar + active-jobs panel (if any).                            |
| `music.yandex.ru/.../track/<id>`             | Single-track row above the search bar.                              |
| `music.yandex.ru/album/<id>`                 | Album header with **Download all** + scrollable track list.         |
| `youtube.com/watch?v=…` / `youtu.be/…`       | Video card with **Audio** + quality-selectable **Video**.           |
| `youtube.com/playlist?list=…` or `&list=…`   | Playlist header with audio/video toggle + **Download all** + entries.|
| `music.youtube.com/...&list=…`               | Same playlist card.                                                 |

The Active downloads panel and the search results coexist below the
search bar; the popup is 600×600.

## Server endpoints used

| Action                | Endpoint                                  |
|-----------------------|--------------------------------------------|
| Health probe          | `GET /health`                             |
| Read settings         | `GET /settings`                           |
| Toggle auto-import    | `PUT /settings`                           |
| Search                | `GET /search?q=…&limit=30`                |
| Yandex track context  | `GET /yandex/track-info?url=…`            |
| Yandex album context  | `GET /yandex/album-info?url=…`            |
| YouTube video context | `GET /youtube/info?url=…`                 |
| YouTube playlist ctx  | `GET /youtube/playlist-info?url=…`        |
| Start download        | `POST /download` / `/youtube/audio`/`/youtube/video` |
| Start group download  | `POST /yandex/album` / `POST /youtube/playlist` |
| Active jobs           | `GET /tasks?active=true`                  |
| Poll a job            | `GET /tasks/{id}` (every ~800 ms)         |
| Cancel a job          | `POST /tasks/{id}/cancel`                 |

The download buttons cycle through
`Queued → Downloading 47% → Converting → Exporting → In library`. Tracks
already in the SQLite library index show **In library** immediately. Click
on a row's title or cover to open it on `music.yandex.ru` or `youtube.com`.

## Files

- `manifest.json` — Manifest V3; needs `tabs` + host permissions for
  `127.0.0.1` / `localhost`.
- `popup.html` / `popup.css` / `popup.js` — the entire popup UI.
- `icons/icon{16,32,48,128}.png` — generated brand icons (replace at will).
