# qwerf — Chrome extension

Search Yandex.Music from a Chrome popup and download tracks straight into your
local lossless library through the `ymsync-server` listener.

## Load it

1. Run the listener once on your Mac: `ymsync-server`.
2. Open `chrome://extensions`, enable **Developer mode**, click **Load unpacked**
   and pick this `extension/` folder.
3. Pin the qwerf icon to your toolbar — the popup is the whole UI.

## Settings

Click the gear icon in the top-right of the popup. The only setting is the
server URL, which defaults to `http://127.0.0.1:8765`. The value is stored in
`chrome.storage.sync` so it follows your Chrome profile.

## How it talks to the server

| Action          | Endpoint                                |
|-----------------|------------------------------------------|
| Health probe    | `GET /health`                           |
| Search          | `GET /search?q=…&limit=30`              |
| Start download  | `POST /download` `{ track_id }`         |
| Poll progress   | `GET /tasks/{id}` (every ~800 ms)       |

The download button cycles through `Queued… → Downloading… → Converting… → In
library`. Tracks already in your library show **In library** immediately; clicks
on a row's title or cover open the song's page on `music.yandex.ru` in a new
tab.

## Files

- `manifest.json` — Manifest V3 declaration
- `popup.html` / `popup.css` / `popup.js` — the entire popup UI
- `icons/icon{16,32,48,128}.png` — generated brand icons (replace at will)
