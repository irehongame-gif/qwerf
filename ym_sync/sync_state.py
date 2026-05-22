"""Sync state management for incremental downloads.

Tracks which songs have been downloaded/exported to avoid re-processing.
"""

import json
from pathlib import Path
from typing import Any, Dict


def load_state(state_path: Path) -> Dict[str, Any]:
    """Load sync state from a JSON file.

    Args:
        state_path: Path to the .sync_state.json file.

    Returns:
        Dictionary mapping track_id (str) to {filename, status}.
    """
    if state_path.is_file():
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state_path: Path, state_dict: Dict[str, Any]) -> None:
    """Save sync state to a JSON file.

    Args:
        state_path: Path to the .sync_state.json file.
        state_dict: Dictionary mapping track_id (str) to {filename, status}.
    """
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state_dict, f, ensure_ascii=False, indent=2)
