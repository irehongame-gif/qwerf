# ymsync-server

A tiny FastAPI app the Chrome extension talks to. Lives on `127.0.0.1` only — it
holds your Yandex.Music token, so it must never be exposed to the network.

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

| Method | Path                       | Purpose                                                |
|--------|----------------------------|--------------------------------------------------------|
| GET    | `/health`                  | liveness check                                          |
| GET    | `/search?q=…&limit=20`     | Yandex search (returns hits with cover + URL)          |
| GET    | `/tracks/{track_id}`       | "is this track already exported?"                       |
| POST   | `/download`                | start a download → convert → export job (`{track_id}`) |
| GET    | `/tasks/{task_id}`         | poll task progress (`stage`, `error`, `exported_path`) |
| GET    | `/tasks`                   | list recent tasks                                      |

A "task" is identified by `track_id`: starting a download for a track that's
already running (or already exported) just returns the existing task.

CORS is enabled for `chrome-extension://*` origins so the popup can call the
server directly via `fetch`.
