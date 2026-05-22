"""Thin wrapper around the Yandex.Music client constructor.

We delegate to ``ymd.core.init_client`` so that the same retry/timeout policy
used by the upstream downloader applies to our own API calls (likes listing,
search, etc.).
"""

from __future__ import annotations

from ymd.core import init_client
from yandex_music import Client


def build_client(
    token: str,
    *,
    timeout: int = 20,
    tries: int = 5,
    retry_delay: int = 5,
) -> Client:
    """Return a fully initialized authenticated Yandex.Music client."""
    return init_client(
        token=token,
        timeout=timeout,
        max_try_count=tries,
        retry_delay=retry_delay,
    )
