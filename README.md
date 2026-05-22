# ym-sync

A CLI tool for syncing your Yandex Music favorites. Downloads tracks in FLAC (lossless) and converts them to ALAC (.m4a) format.

## Prerequisites

- Python 3.9+
- ffmpeg (required for FLAC to ALAC conversion)

### Installing ffmpeg

- **macOS:** `brew install ffmpeg`
- **Ubuntu/Debian:** `sudo apt install ffmpeg`
- **Windows:** Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH

## Installation

```bash
pip install -e .
```

Or install directly:

```bash
pip install .
```

## Getting a Yandex Music Token

You need an OAuth token for the Yandex Music API. You can obtain one by:

1. Going to the Yandex OAuth page
2. Authorizing the application
3. Copying the token from the response

## Usage

```bash
# Using --token argument
ym-sync --token YOUR_TOKEN --output-dir ~/Music

# Using environment variable
export YM_TOKEN=YOUR_TOKEN
ym-sync --output-dir ~/Music

# Using config file (~/.ym_sync_token)
echo "YOUR_TOKEN" > ~/.ym_sync_token
ym-sync --output-dir ~/Music
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--token` | None | Yandex Music OAuth token |
| `--output-dir` | `.` | Output directory for downloaded files |
| `--delay` | `1` | Delay between downloads (seconds) |
| `--timeout` | `20` | Request timeout (seconds) |
| `--max-retries` | `20` | Max retries on network error |
| `--retry-delay` | `5` | Delay between retries (seconds) |
| `--download-only` | off | Only download FLAC, skip ALAC conversion |

### Running as a module

```bash
python -m ym_sync --help
```

## Folder Structure

After syncing, your output directory will look like:

```
output-dir/
  Downloaded/       # FLAC files with full metadata
    Artist - Title.flac
    ...
  Exported/         # ALAC files (flat, no subfolders)
    Artist - Title.m4a
    ...
  .sync_state.json  # Tracks sync progress (do not delete)
```

## Incremental Sync

The tool tracks sync state in `.sync_state.json`. On subsequent runs, only new favorites
will be downloaded. This file maps each track ID to its filename and sync status
(`downloaded` or `exported`).

To re-sync all tracks, delete `.sync_state.json`.
