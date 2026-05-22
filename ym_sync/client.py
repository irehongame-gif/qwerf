"""Yandex Music client initialization with retry logic."""

import time

from yandex_music import Client
from yandex_music.exceptions import NetworkError


def init_client(
    token: str,
    timeout: int = 20,
    max_retries: int = 20,
    retry_delay: int = 5,
) -> Client:
    """Initialize and return a Yandex Music client with retry support.

    Args:
        token: Yandex Music OAuth token.
        timeout: Request timeout in seconds.
        max_retries: Maximum number of retries on network errors. 0 means infinite.
        retry_delay: Delay between retries in seconds.

    Returns:
        Initialized Client instance.
    """
    assert timeout > 0
    assert max_retries >= 0
    assert retry_delay >= 0

    client = Client(token)
    client.request.set_timeout(timeout)

    original_wrapper = client.request._request_wrapper

    def retry_wrapper(*args, **kwargs):
        try_count = 0
        while True:
            try:
                return original_wrapper(*args, **kwargs)
            except NetworkError as error:
                if max_retries == 0 or try_count < max_retries:
                    try_count += 1
                    time.sleep(retry_delay)
                    continue
                raise error

    client.request._request_wrapper = retry_wrapper
    return client.init()
