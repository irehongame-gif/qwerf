# ym-sync

A CLI tool for syncing your Yandex Music favorites as .m4a files. Tracks are downloaded in lossless quality (FLAC-in-MP4) when available, or AAC-in-MP4 otherwise. Both formats are saved as `.m4a` and play natively on macOS without any conversion.

## Prerequisites

- Python 3.9+
- ffmpeg (optional, only needed in the rare case the API returns raw FLAC)

### Installing ffmpeg (optional)

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
| `--download-only` | off | Only download, skip export to Exported/ |

### Running as a module

```bash
python -m ym_sync --help
```

## How It Works

1. The tool requests tracks at lossless quality from the Yandex Music API
2. The API returns FLAC-in-MP4 (lossless .m4a) when available, or AAC-in-MP4 (lossy .m4a) otherwise
3. Files are saved directly as `.m4a` in the `Downloaded/` folder with full metadata tags
4. Files are then copied to the `Exported/` folder (flat, no subfolders)
5. In the rare case the API returns raw FLAC, ffmpeg converts it to ALAC .m4a for export

No ffmpeg conversion is needed in normal operation since both FLAC-in-MP4 and AAC-in-MP4 are already native .m4a files.

## Folder Structure

After syncing, your output directory will look like:

```
output-dir/
  Downloaded/       # .m4a files with full metadata (lossless or AAC)
    Artist - Title [track_id].m4a
    ...
  Exported/         # Copies of .m4a files (flat, no subfolders)
    Artist - Title [track_id].m4a
    ...
  .sync_state.json  # Tracks sync progress (do not delete)
```

## Incremental Sync

The tool tracks sync state in `.sync_state.json`. On subsequent runs, only new favorites
will be downloaded. This file maps each track ID to its filename and sync status
(`downloaded` or `exported`).

To re-sync all tracks, delete `.sync_state.json`.
