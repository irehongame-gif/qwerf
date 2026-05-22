"""CLI entry point for ym-sync."""

import argparse
import os
import sys
from pathlib import Path

from ym_sync.client import init_client
from ym_sync.converter import FfmpegNotFoundError, convert_to_alac
from ym_sync.downloader import (
    build_filename,
    download_track_flac,
    fetch_favorites,
    is_track_in_state,
)
from ym_sync.sync_state import load_state, save_state


def get_token(args_token: str = None) -> str:
    """Resolve the authentication token from args, env var, or config file.

    Priority: --token arg > YM_TOKEN env var > ~/.ym_sync_token file.

    Returns:
        The token string.

    Raises:
        SystemExit: If no token can be found.
    """
    if args_token:
        return args_token

    env_token = os.environ.get("YM_TOKEN")
    if env_token:
        return env_token

    token_file = Path.home() / ".ym_sync_token"
    if token_file.is_file():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token

    print("Error: No token provided.", file=sys.stderr)
    print(
        "Provide a token via --token, YM_TOKEN env var, or ~/.ym_sync_token file.",
        file=sys.stderr,
    )
    sys.exit(1)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="ym-sync",
        description="Sync Yandex Music favorites: download FLAC, convert to ALAC (.m4a)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "--token",
        metavar="TOKEN",
        default=None,
        help="Yandex Music OAuth token (or set YM_TOKEN env var, or ~/.ym_sync_token)",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        default=".",
        help="Output directory (default: current directory)",
    )
    parser.add_argument(
        "--delay",
        metavar="SECONDS",
        type=float,
        default=1.0,
        help="Delay between downloads in seconds (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=int,
        default=20,
        help="Request timeout in seconds (default: 20)",
    )
    parser.add_argument(
        "--max-retries",
        metavar="N",
        type=int,
        default=20,
        help="Max retries on network error (default: 20, 0=infinite)",
    )
    parser.add_argument(
        "--retry-delay",
        metavar="SECONDS",
        type=int,
        default=5,
        help="Delay between retries in seconds (default: 5)",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Only download FLAC files, skip ALAC conversion",
    )

    args = parser.parse_args()

    token = get_token(args.token)

    output_dir = Path(args.output_dir)
    downloaded_dir = output_dir / "Downloaded"
    exported_dir = output_dir / "Exported"
    state_path = output_dir / ".sync_state.json"

    # Load existing sync state
    state = load_state(state_path)

    # Initialize client
    print("Initializing Yandex Music client...")
    try:
        client = init_client(
            token=token,
            timeout=args.timeout,
            max_retries=args.max_retries,
            retry_delay=args.retry_delay,
        )
    except Exception as e:
        print(f"Error: Failed to initialize client: {e}", file=sys.stderr)
        sys.exit(1)

    # Fetch favorites
    print("Fetching favorite tracks...")
    try:
        tracks = fetch_favorites(client)
    except Exception as e:
        print(f"Error: Failed to fetch favorites: {e}", file=sys.stderr)
        sys.exit(1)

    if not tracks:
        print("No favorite tracks found.")
        return

    total = len(tracks)
    print(f"Found {total} favorite track(s).")

    # Process each track
    downloaded_count = 0
    skipped_count = 0
    error_count = 0

    for i, track in enumerate(tracks, 1):
        track_id = str(track.id)
        filename = build_filename(track)

        # Skip if already in state
        if is_track_in_state(track_id, state):
            skipped_count += 1
            continue

        if not track.available:
            print(f"[{i}/{total}] Skipping (unavailable): {filename}")
            error_count += 1
            continue

        print(f"[{i}/{total}] Downloading: {filename}")

        try:
            flac_path = download_track_flac(
                client=client,
                track=track,
                downloaded_dir=downloaded_dir,
                delay=args.delay,
            )
        except Exception as e:
            print(f"  Warning: Failed to download: {e}", file=sys.stderr)
            error_count += 1
            continue

        # Update state as downloaded
        state[track_id] = {"filename": filename, "status": "downloaded"}
        save_state(state_path, state)

        # Convert to ALAC unless download-only mode
        if not args.download_only:
            try:
                alac_path = convert_to_alac(flac_path, exported_dir)
                state[track_id]["status"] = "exported"
                save_state(state_path, state)
                print(f"  Exported: {alac_path.name}")
            except FfmpegNotFoundError:
                print(
                    "  Warning: ffmpeg not found, skipping ALAC conversion.",
                    file=sys.stderr,
                )
                print(
                    "  Install ffmpeg to enable conversion. FLAC file was saved.",
                    file=sys.stderr,
                )
            except Exception as e:
                print(f"  Warning: Conversion failed: {e}", file=sys.stderr)

        downloaded_count += 1

    print(f"\nSync complete: {downloaded_count} downloaded, "
          f"{skipped_count} skipped (already synced), {error_count} errors.")


if __name__ == "__main__":
    main()
