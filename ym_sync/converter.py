"""FLAC to ALAC conversion via ffmpeg."""

import shutil
import subprocess
from pathlib import Path


class FfmpegNotFoundError(RuntimeError):
    """Raised when ffmpeg is not available on the system."""

    def __init__(self):
        super().__init__(
            "ffmpeg is not installed or not found in PATH. "
            "Please install ffmpeg to enable FLAC to ALAC conversion."
        )


def check_ffmpeg_available() -> bool:
    """Check if ffmpeg is available on the system."""
    return shutil.which("ffmpeg") is not None


def convert_to_alac(flac_path: Path, exported_dir: Path) -> Path:
    """Convert a FLAC file to ALAC (.m4a) format using ffmpeg.

    Args:
        flac_path: Path to the source FLAC file.
        exported_dir: Directory where the .m4a file will be placed.

    Returns:
        Path to the output .m4a file.

    Raises:
        FfmpegNotFoundError: If ffmpeg is not installed.
        subprocess.CalledProcessError: If ffmpeg conversion fails.
    """
    if not check_ffmpeg_available():
        raise FfmpegNotFoundError()

    exported_dir.mkdir(parents=True, exist_ok=True)

    output_name = flac_path.stem + ".m4a"
    output_path = exported_dir / output_name

    cmd = [
        "ffmpeg",
        "-i", str(flac_path),
        "-acodec", "alac",
        "-y",
        str(output_path),
    ]

    subprocess.run(cmd, check=True, capture_output=True)
    return output_path
