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

## HTTP API

| Method | Path                       | Purpose                                                     |
|--------|----------------------------|--------------------------------------------------------------|
| GET    | `/health`                  | liveness check                                                |
| GET    | `/search?q=…&limit=20`     | Yandex search (returns hits with cover + URL + library flag)  |
| GET    | `/tracks/{track_id}`       | "is this Yandex track already exported?"                      |
| GET    | `/yandex/track-info`       | single Yandex track (`?url=` or `?track_id=`)                |
| POST   | `/download`                | start a Yandex track download → convert → export              |
| GET    | `/youtube/info?url=…`      | yt-dlp metadata + available video heights                     |
| POST   | `/youtube/audio`           | start YouTube audio download (`{ url }`)                      |
| POST   | `/youtube/video`           | start YouTube video download (`{ url, height }`)              |
| GET    | `/tasks/{task_id}`         | poll any job (yandex / youtube audio / youtube video)         |
| GET    | `/tasks`                   | list recent jobs                                              |

A "task" is identified by `(kind, dedup_key)`:

| Kind             | dedup key                         |
|------------------|------------------------------------|
| `yandex_track`   | `<track_id>`                       |
| `youtube_audio`  | `<url>`                            |
| `youtube_video`  | `<url>#h=<height>`                 |

Asking to download something that's already running (or already exported)
returns the existing task — no duplicate work.

CORS is enabled for `chrome-extension://*` origins so the popup can call the
server directly via `fetch`.
