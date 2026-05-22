"""Configuration loading.

Config is read from ``~/.config/ymsync/config.toml`` (or ``$YMSYNC_CONFIG``).
The token may also be supplied via the ``YMSYNC_TOKEN`` env var, which takes
precedence over the file value.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib  # type: ignore[import-not-found]
else:
    import tomli as tomllib  # type: ignore[no-redef]

import tomli_w
from platformdirs import user_config_dir

CONFIG_DIR = Path(user_config_dir("ymsync"))
CONFIG_FILE = CONFIG_DIR / "config.toml"

DEFAULT_DOWNLOAD_DIR = Path.home() / "Music" / "qwerf" / "Downloaded"
DEFAULT_EXPORT_DIR = Path.home() / "Music" / "qwerf" / "Exported"


class ConfigError(RuntimeError):
    """Raised when the config file is missing or invalid."""


@dataclass
class Config:
    token: str
    download_dir: Path = field(default_factory=lambda: DEFAULT_DOWNLOAD_DIR)
    export_dir: Path = field(default_factory=lambda: DEFAULT_EXPORT_DIR)
    quality: str = "lossless"  # lossless | normal | low
    ffmpeg_path: str = "ffmpeg"
    server_host: str = "127.0.0.1"
    server_port: int = 8765

    def __post_init__(self) -> None:
        self.download_dir = Path(self.download_dir).expanduser()
        self.export_dir = Path(self.export_dir).expanduser()
        if self.quality not in {"lossless", "normal", "low"}:
            raise ConfigError(
                f"Invalid quality {self.quality!r}; expected lossless|normal|low"
            )

    def ensure_dirs(self) -> None:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)


def _config_path() -> Path:
    override = os.environ.get("YMSYNC_CONFIG")
    return Path(override).expanduser() if override else CONFIG_FILE


def load_config(path: Optional[Path] = None) -> Config:
    """Load the config from disk, with env var overrides applied."""
    target = path or _config_path()
    data: dict = {}
    if target.is_file():
        with target.open("rb") as fh:
            data = tomllib.load(fh)
    if env_token := os.environ.get("YMSYNC_TOKEN"):
        data["token"] = env_token
    if "token" not in data or not data["token"]:
        raise ConfigError(
            f"No Yandex.Music token. Run `ymsync init` or set YMSYNC_TOKEN. "
            f"Looked at {target}"
        )
    return Config(**{k: v for k, v in data.items() if k in Config.__annotations__})


def write_config(cfg: Config, path: Optional[Path] = None) -> Path:
    """Persist the given config to disk and return the path written."""
    target = path or _config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": cfg.token,
        "download_dir": str(cfg.download_dir),
        "export_dir": str(cfg.export_dir),
        "quality": cfg.quality,
        "ffmpeg_path": cfg.ffmpeg_path,
        "server_host": cfg.server_host,
        "server_port": cfg.server_port,
    }
    with target.open("wb") as fh:
        tomli_w.dump(payload, fh)
    return target
