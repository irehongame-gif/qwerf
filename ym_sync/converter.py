"""File conversion and export utilities.

Handles copying .m4a files to the Exported folder, and converting raw FLAC
to ALAC (.m4a) via ffmpeg as a fallback.
"""

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


def copy_to_exported(src_path: Path, exported_dir: Path) -> Path:
    """Copy a file (typically .m4a) from Downloaded to Exported directory.

    The Exported directory is flat (no subfolders). The filename is
    preserved as-is.

    Args:
        src_path: Path to the source file in Downloaded/.
        exported_dir: Path to the Exported/ directory.

    Returns:
        Path to the copied file in Exported/.
    """
    exported_dir.mkdir(parents=True, exist_ok=True)
    dest_path = exported_dir / src_path.name
    shutil.copy2(str(src_path), str(dest_path))
    return dest_path


def convert_flac_to_alac(flac_path: Path, exported_dir: Path) -> Path:
    """Convert a raw FLAC file to ALAC (.m4a) format using ffmpeg.

    This is a fallback for the rare case when the API returns raw FLAC
    instead of FLAC-in-MP4. In normal operation, tracks arrive as .m4a
    and only need to be copied.

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
