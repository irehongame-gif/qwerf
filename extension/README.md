# qwerf — Chrome extension

Search Yandex.Music from a Chrome popup, **plus**:

- One-click download when the active tab is a `music.yandex.ru/track/…` page.
- One-click audio download **and** quality-selectable video download when the
  active tab is a YouTube video.

All downloads are routed through the local `ymsync-server` listener.

## Load it

1. Run the listener once on your Mac: `ymsync-server`.
2. Open `chrome://extensions`, enable **Developer mode**, click **Load unpacked**
   and pick this `extension/` folder.
3. Pin the qwerf icon to your toolbar — the popup is the whole UI.

## Settings

Click the gear icon in the top-right of the popup. The only setting is the
server URL, which defaults to `http://127.0.0.1:8765`. The value is stored in
`chrome.storage.sync` so it follows your Chrome profile.

## What you'll see

| Active tab                          | Popup shows                                                          |
|-------------------------------------|----------------------------------------------------------------------|
| Anywhere else                       | Just the search bar.                                                  |
| `music.yandex.ru/.../track/<id>`    | Track row (cover + title + Download) above the search bar.            |
| `youtube.com/watch?v=…` / `youtu.be/…` | YouTube card with thumbnail, title, channel, **Audio** + quality-selectable **Video**. |

The page-context card stays visible even while you also use the search bar
below it, so you can grab the page's track and run a quick search in the same
popup session.

## How it talks to the server

| Action          | Endpoint                                |
|-----------------|------------------------------------------|
| Health probe    | `GET /health`                           |
| Search          | `GET /search?q=…&limit=30`              |
| Yandex context  | `GET /yandex/track-info?url=…`          |
| YouTube context | `GET /youtube/info?url=…`               |
| Start download  | `POST /download` / `/youtube/audio` / `/youtube/video` |
| Poll progress   | `GET /tasks/{id}` (every ~800 ms)       |

The download buttons cycle through `Queued… → Downloading 47% → Converting…
→ In library`. Tracks already in your library (audio side) show **In library**
immediately. Click on a row's title or cover to open the song's page on
`music.yandex.ru` (or YouTube) in a new tab.

## Files

- `manifest.json` — Manifest V3; needs `tabs` to read the active URL and
  `host_permissions` for `127.0.0.1` / `localhost`.
- `popup.html` / `popup.css` / `popup.js` — the entire popup UI.
- `icons/icon{16,32,48,128}.png` — generated brand icons (replace at will).
