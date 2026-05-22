"""SQLite-backed library index.

Maps each known audio file to its metadata so the listener can answer
"is this song already in my library?" in O(1) without re-reading every
audio file's tags on every search.

The index lives at :attr:`Config.db_path` (default
``~/.local/share/ymsync/library.db``). The schema is intentionally tiny —
just enough to do (title, artists) lookups while supporting incremental
re-scans of the Downloaded / Exported folders.

Concurrency
-----------
* Reads are safe from many threads in parallel (we open a fresh connection
  per call and rely on WAL mode for non-blocking readers).
* Writes go through a single :class:`threading.Lock` per :class:`LibraryIndex`
  instance — SQLite serialises them anyway and we'd rather see a clean
  ``acquire`` than a ``database is locked`` error.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from ymsync.audio_inspect import (
    detect_codec,
    normalize,
    normalize_artists,
    read_metadata,
)


_AUDIO_SUFFIXES = {".m4a", ".m4b", ".flac", ".mp3", ".aac", ".ogg", ".opus", ".wav"}

KIND_DOWNLOADED = "downloaded"
KIND_EXPORTED = "exported"


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class IndexRow:
    """One row of the index, projected back into Python land."""

    path: str
    mtime: float
    size: int
    title: Optional[str]
    album: Optional[str]
    artists: List[str]
    title_raw: Optional[str]
    album_raw: Optional[str]
    artists_raw: List[str]
    codec: str
    kind: str
    indexed_at: float

    def exists(self) -> bool:
        return Path(self.path).is_file()


@dataclass
class ScanResult:
    """Summary of one directory scan."""

    scanned: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    errors: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,
    mtime       REAL    NOT NULL,
    size        INTEGER NOT NULL,
    title       TEXT,                 -- normalised for lookup
    album       TEXT,                 -- normalised for lookup
    artists     TEXT,                 -- "alpha | beta" sorted, normalised
    title_raw   TEXT,
    album_raw   TEXT,
    artists_raw TEXT,                 -- JSON array
    codec       TEXT,
    kind        TEXT    NOT NULL DEFAULT 'downloaded',
    indexed_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_lookup  ON files(title, artists);
CREATE INDEX IF NOT EXISTS idx_files_kind    ON files(kind);
CREATE INDEX IF NOT EXISTS idx_files_album   ON files(album);
"""


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class LibraryIndex:
    """Read/write façade over the SQLite file."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # Per-call connection: SQLite recommends one connection per thread,
        # this is the simplest way to honour that without thread-local state.
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,        # autocommit; we wrap writes in BEGIN/COMMIT
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            for stmt in _SCHEMA.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)

    # ------------------------------------------------------------------
    # Single-row CRUD
    # ------------------------------------------------------------------

    def upsert_file(
        self,
        path: Path,
        *,
        title: Optional[str] = None,
        album: Optional[str] = None,
        artists: Optional[Iterable[str]] = None,
        codec: Optional[str] = None,
        kind: str = KIND_DOWNLOADED,
        mtime: Optional[float] = None,
        size: Optional[int] = None,
        indexed_at: Optional[float] = None,
    ) -> IndexRow:
        """Insert or replace a row for ``path``. Reads metadata + codec from
        the file when not supplied (so callers can hand in only what they
        already know cheaply).
        """
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)

        if mtime is None or size is None:
            stat = path.stat()
            mtime = mtime if mtime is not None else stat.st_mtime
            size = size if size is not None else stat.st_size

        if title is None and album is None and not artists:
            tags = read_metadata(path)
            title = tags.title
            album = tags.album
            artists = tags.artists

        if codec is None:
            codec = detect_codec(path)

        artists_list = list(artists or [])
        norm_title = normalize(title) or None
        norm_album = normalize(album) or None
        norm_artists = normalize_artists(artists_list) or None
        artists_raw_json = json.dumps(artists_list, ensure_ascii=False)

        from time import time as _now
        indexed_at = indexed_at if indexed_at is not None else _now()

        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO files
                  (path, mtime, size,
                   title, album, artists,
                   title_raw, album_raw, artists_raw,
                   codec, kind, indexed_at)
                VALUES
                  (:path, :mtime, :size,
                   :title, :album, :artists,
                   :title_raw, :album_raw, :artists_raw,
                   :codec, :kind, :indexed_at)
                ON CONFLICT(path) DO UPDATE SET
                   mtime       = excluded.mtime,
                   size        = excluded.size,
                   title       = excluded.title,
                   album       = excluded.album,
                   artists     = excluded.artists,
                   title_raw   = excluded.title_raw,
                   album_raw   = excluded.album_raw,
                   artists_raw = excluded.artists_raw,
                   codec       = excluded.codec,
                   kind        = excluded.kind,
                   indexed_at  = excluded.indexed_at
                """,
                {
                    "path": str(path),
                    "mtime": mtime,
                    "size": size,
                    "title": norm_title,
                    "album": norm_album,
                    "artists": norm_artists,
                    "title_raw": title,
                    "album_raw": album,
                    "artists_raw": artists_raw_json,
                    "codec": codec,
                    "kind": kind,
                    "indexed_at": indexed_at,
                },
            )
            conn.execute("COMMIT")

        return IndexRow(
            path=str(path),
            mtime=mtime,
            size=size,
            title=norm_title,
            album=norm_album,
            artists=[a for a in (norm_artists or "").split(" | ") if a],
            title_raw=title,
            album_raw=album,
            artists_raw=artists_list,
            codec=codec or "",
            kind=kind,
            indexed_at=indexed_at,
        )

    def remove_file(self, path: Path | str) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM files WHERE path = ?", (str(path),))
        return cur.rowcount > 0

    def get(self, path: Path | str) -> Optional[IndexRow]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM files WHERE path = ?", (str(path),)
            ).fetchone()
        return _row_to_index(row) if row else None

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def find_by_metadata(
        self,
        title: Optional[str],
        album: Optional[str],
        artists: Iterable[str],
        *,
        kinds: Optional[Iterable[str]] = None,
    ) -> List[IndexRow]:
        """Return rows whose normalised (title, artists) match.

        Album is used only as a tiebreaker — if more than one row matches and
        any of them carries the same album, only those are returned.

        ``kinds`` filters by row kind; ``None`` means any kind.
        """
        norm_title = normalize(title)
        norm_artists = normalize_artists(list(artists or []))
        if not norm_title and not norm_artists:
            return []

        clauses = []
        params: list = []
        if norm_title:
            clauses.append("title = ?")
            params.append(norm_title)
        if norm_artists:
            clauses.append("artists = ?")
            params.append(norm_artists)
        if kinds:
            kinds_list = list(kinds)
            placeholders = ",".join("?" * len(kinds_list))
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds_list)

        sql = "SELECT * FROM files WHERE " + " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        results = [_row_to_index(r) for r in rows]
        if len(results) <= 1:
            return results

        norm_album = normalize(album)
        if norm_album:
            tied = [r for r in results if r.album == norm_album]
            if tied:
                return tied
        return results

    def has_metadata(
        self,
        title: Optional[str],
        album: Optional[str],
        artists: Iterable[str],
        *,
        kinds: Optional[Iterable[str]] = None,
    ) -> bool:
        """Same matching rules as :meth:`find_by_metadata` but only returns
        ``True`` once we've also verified the file is still on disk."""
        for row in self.find_by_metadata(title, album, artists, kinds=kinds):
            if row.exists():
                return True
        return False

    def all_rows(self, kind: Optional[str] = None) -> List[IndexRow]:
        sql = "SELECT * FROM files"
        params: tuple = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            params = (kind,)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_index(r) for r in rows]

    # ------------------------------------------------------------------
    # Bulk scanning
    # ------------------------------------------------------------------

    def scan_directory(
        self,
        directory: Path,
        kind: str,
        *,
        parallel: int = 4,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> ScanResult:
        """Walk ``directory`` for audio files and bring the index up to date.

        Skips files whose ``(mtime, size)`` are already recorded, so a re-scan
        of a 10k-track library after one new download is fast.

        ``parallel`` controls how many worker threads do tag-reading in
        parallel — each thread spawns at most one ffprobe at a time, so the
        wall-clock win on many files is significant.
        """
        directory = Path(directory).expanduser()
        result = ScanResult()
        if not directory.is_dir():
            return result

        # 1. Collect candidate files.
        candidates: List[Path] = []
        for entry in directory.rglob("*"):
            if entry.is_file() and entry.suffix.lower() in _AUDIO_SUFFIXES:
                candidates.append(entry)

        # 2. Pre-load existing rows (path -> (mtime, size)) for skip-detection.
        with self._connect() as conn:
            existing_rows = conn.execute(
                "SELECT path, mtime, size FROM files WHERE kind = ?",
                (kind,),
            ).fetchall()
        existing = {
            r["path"]: (r["mtime"], r["size"]) for r in existing_rows
        }
        seen: set[str] = set()

        # 3. Determine which files actually need a re-read.
        to_read: List[Path] = []
        for path in candidates:
            spath = str(path)
            seen.add(spath)
            try:
                stat = path.stat()
            except OSError as exc:
                result.errors.append(f"{path}: {exc}")
                continue
            old = existing.get(spath)
            if old is not None and old[0] == stat.st_mtime and old[1] == stat.st_size:
                result.unchanged += 1
                result.scanned += 1
                continue
            to_read.append(path)

        # 4. Read tags in parallel, then upsert sequentially (cheap).
        if to_read:
            parallel = max(1, parallel)
            with ThreadPoolExecutor(
                max_workers=parallel, thread_name_prefix="ymsync-scan"
            ) as pool:
                futs = {
                    pool.submit(_extract_for_index, p): p for p in to_read
                }
                for fut in as_completed(futs):
                    p = futs[fut]
                    try:
                        meta = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        result.errors.append(f"{p}: {exc}")
                        result.scanned += 1
                        continue
                    is_new = str(p) not in existing
                    self.upsert_file(
                        p,
                        title=meta["title"],
                        album=meta["album"],
                        artists=meta["artists"],
                        codec=meta["codec"],
                        kind=kind,
                        mtime=meta["mtime"],
                        size=meta["size"],
                    )
                    if is_new:
                        result.inserted += 1
                    else:
                        result.updated += 1
                    result.scanned += 1
                    if on_progress is not None:
                        on_progress(result.scanned, len(candidates))

        # 5. Drop rows for files that no longer exist in this directory.
        stale = [p for p in existing if p not in seen and p.startswith(str(directory))]
        if stale:
            with self._write_lock, self._connect() as conn:
                conn.execute("BEGIN")
                conn.executemany(
                    "DELETE FROM files WHERE path = ?",
                    [(p,) for p in stale],
                )
                conn.execute("COMMIT")
            result.removed = len(stale)

        return result

    def verify_existence(self, kind: Optional[str] = None) -> int:
        """Drop rows whose path is gone. Returns the number of rows removed."""
        rows = self.all_rows(kind=kind)
        gone = [r.path for r in rows if not Path(r.path).is_file()]
        if not gone:
            return 0
        with self._write_lock, self._connect() as conn:
            conn.execute("BEGIN")
            conn.executemany(
                "DELETE FROM files WHERE path = ?", [(p,) for p in gone]
            )
            conn.execute("COMMIT")
        return len(gone)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _row_to_index(row: sqlite3.Row) -> IndexRow:
    raw_artists_json = row["artists_raw"] or "[]"
    try:
        artists_raw = json.loads(raw_artists_json)
        if not isinstance(artists_raw, list):
            artists_raw = [str(artists_raw)]
    except json.JSONDecodeError:
        artists_raw = []
    artists_norm = [
        a for a in (row["artists"] or "").split(" | ") if a
    ]
    return IndexRow(
        path=row["path"],
        mtime=row["mtime"],
        size=row["size"],
        title=row["title"],
        album=row["album"],
        artists=artists_norm,
        title_raw=row["title_raw"],
        album_raw=row["album_raw"],
        artists_raw=artists_raw,
        codec=row["codec"] or "",
        kind=row["kind"],
        indexed_at=row["indexed_at"],
    )


def _extract_for_index(path: Path) -> dict:
    """Read everything we need for one row, off the main thread."""
    stat = path.stat()
    tags = read_metadata(path)
    codec = detect_codec(path)
    return {
        "title": tags.title,
        "album": tags.album,
        "artists": tags.artists,
        "codec": codec,
        "mtime": stat.st_mtime,
        "size": stat.st_size,
    }
