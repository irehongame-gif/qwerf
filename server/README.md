# ymsync-server

A tiny FastAPI app the Chrome extension talks to. Lives on `127.0.0.1` only —
it holds your Yandex.Music token, so it must never be exposed to the network.

## Install

The server depends on the CLI's `ymsync` package; install both:

```bash
pip install -e ../cli
pip install -e .
```

## Run

```bash
ymsync-server                       # 127.0.0.1:8765 (defaults from your config)
ymsync-server --host 127.0.0.1 --port 9000
```

The server reuses `~/.config/ymsync/config.toml`, so `ymsync init` is a
prerequisite.

On startup the server warm-scans your Downloaded + Exported folders into the
SQLite index in the background so the popup's "In library" badge is accurate
without re-reading audio tags every time.

## HTTP API

| Method | Path                                  | Purpose                                                     |
|--------|----------------------------------------|--------------------------------------------------------------|
| GET    | `/health`                             | liveness check                                                |
| GET    | `/settings`                           | current runtime-tunable settings                              |
| PUT    | `/settings`                           | update `auto_import_to_music`                                 |
| GET    | `/search?q=…&limit=20`                | Yandex search (returns hits with cover + URL + library flag) |
| GET    | `/tracks/{track_id}`                  | "is this Yandex track already exported?"                     |
| GET    | `/yandex/track-info?url=…`            | single Yandex track context (one row)                        |
| GET    | `/yandex/album-info?url=…`            | full album: summary + every track                            |
| POST   | `/download`                           | enqueue Yandex track download                                 |
| POST   | `/yandex/album`                       | enqueue every track in an album (returns group_id + tasks)    |
| GET    | `/youtube/info?url=…`                 | yt-dlp metadata + available video heights                     |
| GET    | `/youtube/playlist-info?url=…`        | flat playlist enumeration                                     |
| POST   | `/youtube/audio`                      | enqueue YouTube audio download                                |
| POST   | `/youtube/video`                      | enqueue YouTube video at `<= height` px                       |
| POST   | `/youtube/playlist`                   | enqueue every entry in a playlist (audio or video)            |
| GET    | `/tasks/{task_id}`                    | poll any job                                                  |
| GET    | `/tasks?active=true&group=<id>`       | list jobs (use `active=true` for popup re-attach)             |
| POST   | `/tasks/{task_id}/cancel`             | cancel a queued or running job                                |
| POST   | `/tasks/groups/{group_id}/cancel`     | cancel a whole album/playlist group                           |

### Job kinds + dedup keys

| Kind             | dedup key                         |
|------------------|------------------------------------|
| `yandex_track`   | `<track_id>`                       |
| `youtube_audio`  | `<url>`                            |
| `youtube_video`  | `<url>#h=<height>`                 |

Asking to download something that's already running (or finished within the
last 5 min) returns the existing task — no duplicate work.

### Rate limits + queue

* Yandex.Music: max **2** concurrent downloads.
* YouTube / YouTube Music: max **3** concurrent.

Anything beyond the cap stays in `stage="queued"` until a slot opens. Tasks
expose `cancel_event` internally; `POST /tasks/{id}/cancel` flips it and the
pipeline + yt-dlp progress hook bail out at the next safe point.

CORS is enabled for `chrome-extension://*` origins so the popup can call the
server directly via `fetch`.
