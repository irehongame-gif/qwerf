"""ymsync — sync Yandex.Music "My Favorites" to a local lossless ALAC library.

This package is shared between the CLI (`ymsync.cli`) and the listener server
(`ymsync_server.app`). The most useful public surface lives in:

* :mod:`ymsync.config` — typed config loading
* :mod:`ymsync.client` — Yandex client factory
* :mod:`ymsync.pipeline` — the download → convert → export pipeline
* :mod:`ymsync.search` — search wrapper used by the server
"""

from ymsync.config import Config, load_config  # noqa: F401
from ymsync.pipeline import TrackResult, TrackStage, process_track  # noqa: F401

__version__ = "0.1.0"
