"""SQLite-backed state store for tracking upload progress.

Design principles:
  - WAL journal mode for crash-safety without full-file rewrites
  - threading.Lock around all writes for safe concurrent access from worker threads
  - Schema migrations via schema_version table (future-proof)
  - Idempotent operations: upsert_file uses INSERT OR REPLACE on path
"""
from __future__ import annotations

import datetime
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from uploader.errors import StateCorruptedError, StateError
from uploader.models import FileRecord, FileStatus, RunStats


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 1

_DDL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER NOT NULL,
    applied_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS files (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    path                  TEXT    NOT NULL UNIQUE,
    content_hash          TEXT,
    status                TEXT    NOT NULL DEFAULT 'pending'
                          CHECK(status IN ('pending','in_progress','uploaded','error','skipped')),
    upload_token          TEXT,
    google_media_id       TEXT,
    attempts              INTEGER NOT NULL DEFAULT 0,
    error_msg             TEXT,
    original_size_bytes   INTEGER,
    compressed_size_bytes INTEGER,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_files_status
    ON files(status);

CREATE INDEX IF NOT EXISTS idx_files_content_hash
    ON files(content_hash)
    WHERE content_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    exit_reason TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------

class StateStore:
    """Thread-safe wrapper around the SQLite state database."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open_connection()
        self._initialize()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _open_connection(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,   # we manage thread-safety via _lock
                timeout=30.0,
            )
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as e:
            raise StateError(f"Could not open state database at {self._db_path}: {e}") from e

    def _initialize(self) -> None:
        """Create tables and verify schema integrity."""
        try:
            self._conn.executescript(_DDL)
            self._conn.commit()
            # Insert schema version if not present
            cur = self._conn.execute("SELECT COUNT(*) FROM schema_version")
            if cur.fetchone()[0] == 0:
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,)
                )
                self._conn.commit()
        except sqlite3.DatabaseError as e:
            raise StateCorruptedError(
                f"State database at {self._db_path} appears corrupted: {e}"
            ) from e

    def check_integrity(self) -> bool:
        """Return True if the database passes SQLite integrity check."""
        try:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
            return row[0] == "ok"
        except sqlite3.Error:
            return False

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------
    # Write operations (all guarded by _lock)
    # ------------------------------------------------------------------

    def upsert_file(self, record: FileRecord) -> int:
        """Insert or replace a file record. Returns the row id."""
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO files
                        (path, content_hash, status, upload_token, google_media_id,
                         attempts, error_msg, original_size_bytes, compressed_size_bytes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        content_hash          = excluded.content_hash,
                        status                = excluded.status,
                        upload_token          = excluded.upload_token,
                        google_media_id       = excluded.google_media_id,
                        attempts              = excluded.attempts,
                        error_msg             = excluded.error_msg,
                        original_size_bytes   = excluded.original_size_bytes,
                        compressed_size_bytes = excluded.compressed_size_bytes,
                        updated_at            = datetime('now')
                    """,
                    (
                        record.path,
                        record.content_hash,
                        record.status.value,
                        record.upload_token,
                        record.google_media_id,
                        record.attempts,
                        record.error_msg,
                        record.original_size_bytes,
                        record.compressed_size_bytes,
                    ),
                )
                self._conn.commit()
                return cur.lastrowid or self._get_id_for_path(record.path)
            except sqlite3.Error as e:
                self._conn.rollback()
                raise StateError(f"Failed to upsert file record: {e}") from e

    def set_status(
        self,
        file_id: int,
        status: FileStatus,
        *,
        error_msg: Optional[str] = None,
        upload_token: Optional[str] = None,
        google_media_id: Optional[str] = None,
        attempts_increment: int = 0,
        original_size_bytes: Optional[int] = None,
        compressed_size_bytes: Optional[int] = None,
    ) -> None:
        """Atomic status update with optional field changes."""
        with self._lock:
            try:
                updates = ["status = ?", "updated_at = datetime('now')"]
                params: list = [status.value]

                if error_msg is not None:
                    updates.append("error_msg = ?")
                    params.append(error_msg)
                if upload_token is not None:
                    updates.append("upload_token = ?")
                    params.append(upload_token)
                if google_media_id is not None:
                    updates.append("google_media_id = ?")
                    params.append(google_media_id)
                    updates.append("upload_token = NULL")   # clear transient token after success
                if attempts_increment:
                    updates.append("attempts = attempts + ?")
                    params.append(attempts_increment)
                if original_size_bytes is not None:
                    updates.append("original_size_bytes = ?")
                    params.append(original_size_bytes)
                if compressed_size_bytes is not None:
                    updates.append("compressed_size_bytes = ?")
                    params.append(compressed_size_bytes)

                params.append(file_id)
                self._conn.execute(
                    f"UPDATE files SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
                self._conn.commit()
            except sqlite3.Error as e:
                self._conn.rollback()
                raise StateError(f"Failed to update file status: {e}") from e

    def revert_in_progress_to_pending(self) -> int:
        """
        Reset any IN_PROGRESS rows to PENDING and clear upload tokens.
        Called at startup to recover from an interrupted previous run.
        Returns the number of rows reverted.
        """
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    UPDATE files
                    SET status = 'pending', upload_token = NULL, updated_at = datetime('now')
                    WHERE status = 'in_progress'
                    """
                )
                self._conn.commit()
                return cur.rowcount
            except sqlite3.Error as e:
                self._conn.rollback()
                raise StateError(f"Failed to revert in-progress files: {e}") from e

    def clear_all(self) -> None:
        """Delete all file records and metadata (used when user chooses 'start over')."""
        with self._lock:
            try:
                self._conn.execute("DELETE FROM files")
                self._conn.execute("DELETE FROM meta")
                self._conn.commit()
            except sqlite3.Error as e:
                self._conn.rollback()
                raise StateError(f"Failed to clear state: {e}") from e

    def set_meta(self, key: str, value: str) -> None:
        """Persist a key/value pair in the meta table (upsert)."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
                self._conn.commit()
            except sqlite3.Error as e:
                self._conn.rollback()
                raise StateError(f"Failed to set meta '{key}': {e}") from e

    def get_meta(self, key: str) -> Optional[str]:
        """Return the stored value for key, or None if not present."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def start_run(self) -> int:
        """Insert a runs row and return its id."""
        with self._lock:
            try:
                cur = self._conn.execute("INSERT INTO runs DEFAULT VALUES")
                self._conn.commit()
                return cur.lastrowid
            except sqlite3.Error as e:
                self._conn.rollback()
                raise StateError(f"Failed to start run record: {e}") from e

    def finish_run(self, run_id: int, exit_reason: str) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE runs SET finished_at = datetime('now'), exit_reason = ? WHERE id = ?",
                    (exit_reason, run_id),
                )
                self._conn.commit()
            except sqlite3.Error as e:
                self._conn.rollback()
                raise StateError(f"Failed to finish run record: {e}") from e

    # ------------------------------------------------------------------
    # Read operations (no lock required in WAL mode for reads)
    # ------------------------------------------------------------------

    def has_existing_state(self) -> bool:
        """Return True if there are any file rows in the database."""
        row = self._conn.execute("SELECT COUNT(*) FROM files").fetchone()
        return row[0] > 0

    def has_actionable_state(self) -> bool:
        """Return True if there are pending or error files worth resuming."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM files WHERE status IN ('pending','in_progress','error')"
        ).fetchone()
        return row[0] > 0

    def get_last_run(self) -> Optional[dict]:
        """Return the most recent run record as a dict, or None if no runs recorded."""
        row = self._conn.execute(
            "SELECT started_at, finished_at, exit_reason FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return {
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "exit_reason": row["exit_reason"],
        }

    def get_file_by_hash(self, content_hash: str) -> Optional[FileRecord]:
        """Find an uploaded file by content hash (deduplication check)."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE content_hash = ? AND status = 'uploaded' LIMIT 1",
            (content_hash,),
        ).fetchone()
        return _row_to_record(row) if row else None

    def get_file_by_path(self, path: str) -> Optional[FileRecord]:
        """Return the record for a given path, or None if not found."""
        row = self._conn.execute(
            "SELECT * FROM files WHERE path = ? LIMIT 1", (path,)
        ).fetchone()
        return _row_to_record(row) if row else None

    def get_pending_files(self) -> list[FileRecord]:
        """Return all PENDING files in insertion order."""
        rows = self._conn.execute(
            "SELECT * FROM files WHERE status = 'pending' ORDER BY id"
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get_files_by_status(self, status: FileStatus) -> list[FileRecord]:
        rows = self._conn.execute(
            "SELECT * FROM files WHERE status = ? ORDER BY id",
            (status.value,),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get_stats(self) -> RunStats:
        """Aggregate counts and size totals from the database."""
        # Status counts
        counts: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT status, COUNT(*) as cnt FROM files GROUP BY status"
        ):
            counts[row["status"]] = row["cnt"]

        total = sum(counts.values())

        # Size aggregates (only for processed files)
        size_row = self._conn.execute(
            """
            SELECT
                COALESCE(SUM(original_size_bytes), 0)   AS orig,
                COALESCE(SUM(
                    COALESCE(compressed_size_bytes, original_size_bytes)
                ), 0) AS comp
            FROM files
            WHERE status IN ('uploaded', 'error', 'skipped')
              AND original_size_bytes IS NOT NULL
            """
        ).fetchone()

        # Per-type counts (video extensions identified by file path)
        _VIDEO_LIKE = (
            "lower(path) LIKE '%.mp4' OR lower(path) LIKE '%.mov'"
            " OR lower(path) LIKE '%.avi' OR lower(path) LIKE '%.wmv'"
            " OR lower(path) LIKE '%.mkv' OR lower(path) LIKE '%.3gp'"
            " OR lower(path) LIKE '%.mpg' OR lower(path) LIKE '%.mpeg'"
        )
        type_counts: dict[tuple[str, str], int] = {}
        for row in self._conn.execute(
            f"""
            SELECT
                status,
                CASE WHEN {_VIDEO_LIKE} THEN 'video' ELSE 'photo' END AS media_type,
                COUNT(*) AS cnt
            FROM files
            GROUP BY status, media_type
            """
        ):
            type_counts[(row["status"], row["media_type"])] = row["cnt"]

        def _tc(status: str, mtype: str) -> int:
            return type_counts.get((status, mtype), 0)

        return RunStats(
            total=total,
            uploaded=counts.get("uploaded", 0),
            skipped=counts.get("skipped", 0),
            errors=counts.get("error", 0),
            total_original_bytes=size_row["orig"] if size_row else 0,
            total_compressed_bytes=size_row["comp"] if size_row else 0,
            total_photos=_tc("pending", "photo") + _tc("in_progress", "photo")
                         + _tc("uploaded", "photo") + _tc("skipped", "photo")
                         + _tc("error", "photo"),
            total_videos=_tc("pending", "video") + _tc("in_progress", "video")
                         + _tc("uploaded", "video") + _tc("skipped", "video")
                         + _tc("error", "video"),
            uploaded_photos=_tc("uploaded", "photo"),
            uploaded_videos=_tc("uploaded", "video"),
            skipped_photos=_tc("skipped", "photo"),
            skipped_videos=_tc("skipped", "video"),
            errors_photos=_tc("error", "photo"),
            errors_videos=_tc("error", "video"),
        )

    def _get_id_for_path(self, path: str) -> int:
        row = self._conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# Row → FileRecord conversion
# ---------------------------------------------------------------------------

def _row_to_record(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=row["id"],
        path=row["path"],
        content_hash=row["content_hash"],
        status=FileStatus(row["status"]),
        upload_token=row["upload_token"],
        google_media_id=row["google_media_id"],
        attempts=row["attempts"],
        error_msg=row["error_msg"],
        original_size_bytes=row["original_size_bytes"],
        compressed_size_bytes=row["compressed_size_bytes"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


def _parse_dt(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    return datetime.datetime.fromisoformat(value)
