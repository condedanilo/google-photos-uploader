"""Unit tests for uploader/state.py."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from uploader.models import FileRecord, FileStatus, RunStats
from uploader.state import StateStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()


def _make_record(path: str, status: FileStatus = FileStatus.PENDING,
                 content_hash: str | None = None) -> FileRecord:
    return FileRecord(path=path, status=status, content_hash=content_hash)


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------

class TestUpsertAndRead:
    def test_insert_returns_id(self, store: StateStore):
        rec = _make_record("/photos/a.jpg")
        row_id = store.upsert_file(rec)
        assert row_id > 0

    def test_insert_then_read_by_hash(self, store: StateStore):
        rec = _make_record("/photos/a.jpg", content_hash="abc123")
        store.upsert_file(rec)
        # Not uploaded yet — should NOT be found by hash
        found = store.get_file_by_hash("abc123")
        assert found is None

    def test_uploaded_found_by_hash(self, store: StateStore):
        rec = _make_record("/photos/a.jpg", FileStatus.UPLOADED, content_hash="abc123")
        fid = store.upsert_file(rec)
        found = store.get_file_by_hash("abc123")
        assert found is not None
        assert found.path == "/photos/a.jpg"

    def test_upsert_updates_existing_row(self, store: StateStore):
        rec = _make_record("/photos/a.jpg")
        fid = store.upsert_file(rec)
        # Update: same path, new status
        updated = _make_record("/photos/a.jpg", FileStatus.UPLOADED, content_hash="deadbeef")
        store.upsert_file(updated)
        found = store.get_file_by_hash("deadbeef")
        assert found is not None
        # Should still have same id (upsert on path)
        assert found.path == "/photos/a.jpg"

    def test_upsert_different_paths(self, store: StateStore):
        store.upsert_file(_make_record("/photos/a.jpg"))
        store.upsert_file(_make_record("/photos/b.jpg"))
        pending = store.get_pending_files()
        assert len(pending) == 2


# ---------------------------------------------------------------------------
# set_status
# ---------------------------------------------------------------------------

class TestSetStatus:
    def test_set_uploaded(self, store: StateStore):
        rec = _make_record("/photos/a.jpg")
        fid = store.upsert_file(rec)
        store.set_status(fid, FileStatus.UPLOADED, google_media_id="media-123")
        pending = store.get_pending_files()
        assert pending == []

    def test_set_error_with_message(self, store: StateStore):
        rec = _make_record("/photos/a.jpg")
        fid = store.upsert_file(rec)
        store.set_status(fid, FileStatus.ERROR, error_msg="connection reset")
        errors = store.get_files_by_status(FileStatus.ERROR)
        assert len(errors) == 1
        assert errors[0].error_msg == "connection reset"

    def test_attempts_increment(self, store: StateStore):
        fid = store.upsert_file(_make_record("/photos/a.jpg"))
        store.set_status(fid, FileStatus.IN_PROGRESS, attempts_increment=1)
        store.set_status(fid, FileStatus.IN_PROGRESS, attempts_increment=1)
        rows = store.get_files_by_status(FileStatus.IN_PROGRESS)
        assert rows[0].attempts == 2

    def test_upload_token_cleared_on_success(self, store: StateStore):
        fid = store.upsert_file(_make_record("/photos/a.jpg"))
        store.set_status(fid, FileStatus.IN_PROGRESS, upload_token="tok-abc")
        store.set_status(fid, FileStatus.UPLOADED, google_media_id="media-xyz")
        # After success the upload_token should be None
        rows = store.get_files_by_status(FileStatus.UPLOADED)
        assert rows[0].upload_token is None
        assert rows[0].google_media_id == "media-xyz"


# ---------------------------------------------------------------------------
# revert_in_progress_to_pending
# ---------------------------------------------------------------------------

class TestRevertInProgress:
    def test_reverts_only_in_progress(self, store: StateStore):
        fid_p = store.upsert_file(_make_record("/a.jpg"))
        fid_u = store.upsert_file(_make_record("/b.jpg", FileStatus.UPLOADED, "h1"))
        fid_i = store.upsert_file(_make_record("/c.jpg"))
        store.set_status(fid_i, FileStatus.IN_PROGRESS)

        reverted = store.revert_in_progress_to_pending()

        assert reverted == 1
        assert len(store.get_pending_files()) == 2   # fid_p + fid_i reverted
        assert len(store.get_files_by_status(FileStatus.UPLOADED)) == 1

    def test_clears_upload_token(self, store: StateStore):
        fid = store.upsert_file(_make_record("/a.jpg"))
        store.set_status(fid, FileStatus.IN_PROGRESS, upload_token="stale-tok")
        store.revert_in_progress_to_pending()
        pending = store.get_pending_files()
        assert pending[0].upload_token is None

    def test_no_in_progress_returns_zero(self, store: StateStore):
        store.upsert_file(_make_record("/a.jpg"))
        assert store.revert_in_progress_to_pending() == 0


# ---------------------------------------------------------------------------
# has_existing_state / clear_all
# ---------------------------------------------------------------------------

class TestStateLifecycle:
    def test_empty_store(self, store: StateStore):
        assert store.has_existing_state() is False

    def test_non_empty_store(self, store: StateStore):
        store.upsert_file(_make_record("/a.jpg"))
        assert store.has_existing_state() is True

    def test_clear_all(self, store: StateStore):
        store.upsert_file(_make_record("/a.jpg"))
        store.upsert_file(_make_record("/b.jpg"))
        store.clear_all()
        assert store.has_existing_state() is False
        assert store.get_pending_files() == []


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------

class TestGetStats:
    def test_empty(self, store: StateStore):
        stats = store.get_stats()
        assert stats.total == 0
        assert stats.uploaded == 0
        assert stats.errors == 0

    def test_counts(self, store: StateStore):
        store.upsert_file(_make_record("/a.jpg"))
        store.upsert_file(_make_record("/b.jpg"))
        fid_u = store.upsert_file(_make_record("/c.jpg", FileStatus.UPLOADED, "h1"))
        fid_e = store.upsert_file(_make_record("/d.jpg"))
        store.set_status(fid_e, FileStatus.ERROR, error_msg="failed")

        stats = store.get_stats()
        assert stats.total == 4
        assert stats.uploaded == 1
        assert stats.errors == 1
        assert stats.remaining == 2

    def test_size_aggregation(self, store: StateStore):
        fid = store.upsert_file(_make_record("/a.jpg"))
        store.set_status(
            fid, FileStatus.UPLOADED,
            google_media_id="m1",
            original_size_bytes=1000,
            compressed_size_bytes=400,
        )
        stats = store.get_stats()
        assert stats.total_original_bytes == 1000
        assert stats.total_compressed_bytes == 400
        assert stats.bytes_saved == 600
        assert abs(stats.compression_ratio - 0.6) < 0.001


# ---------------------------------------------------------------------------
# Extension filtering (--media-type)
# ---------------------------------------------------------------------------

class TestExtensionFiltering:
    """Verify that get_pending_files() and get_stats() respect include_extensions."""

    def test_get_pending_files_filters_by_extension(self, store: StateStore):
        store.upsert_file(_make_record("/photos/a.jpg"))
        store.upsert_file(_make_record("/photos/b.png"))
        store.upsert_file(_make_record("/videos/c.mp4"))
        store.upsert_file(_make_record("/videos/d.mov"))

        photos_only = frozenset({".jpg", ".png"})
        pending = store.get_pending_files(include_extensions=photos_only)
        paths = {r.path for r in pending}
        assert paths == {"/photos/a.jpg", "/photos/b.png"}

    def test_get_pending_files_no_filter_returns_all(self, store: StateStore):
        store.upsert_file(_make_record("/a.jpg"))
        store.upsert_file(_make_record("/b.mp4"))

        pending = store.get_pending_files()
        assert len(pending) == 2

    def test_get_stats_filters_by_extension(self, store: StateStore):
        # Mix of photos and videos in various states
        store.upsert_file(_make_record("/a.jpg"))                          # pending photo
        store.upsert_file(_make_record("/b.png"))                          # pending photo
        fid_v = store.upsert_file(_make_record("/c.mp4"))                  # pending video
        fid_up = store.upsert_file(_make_record("/d.jpg", FileStatus.UPLOADED, "h1"))  # uploaded photo

        # Mark the video as error
        store.set_status(fid_v, FileStatus.ERROR, error_msg="failed")

        # Without filter: 4 total (2 pending photos + 1 error video + 1 uploaded photo)
        all_stats = store.get_stats()
        assert all_stats.total == 4
        assert all_stats.errors == 1

        # With photos-only filter: should exclude the .mp4
        photos_only = frozenset({".jpg", ".png"})
        photo_stats = store.get_stats(include_extensions=photos_only)
        assert photo_stats.total == 3
        assert photo_stats.errors == 0
        assert photo_stats.uploaded == 1

    def test_get_stats_size_filtered_by_extension(self, store: StateStore):
        fid_photo = store.upsert_file(_make_record("/a.jpg"))
        store.set_status(fid_photo, FileStatus.UPLOADED, google_media_id="m1",
                         original_size_bytes=1000, compressed_size_bytes=400)
        fid_video = store.upsert_file(_make_record("/b.mp4"))
        store.set_status(fid_video, FileStatus.UPLOADED, google_media_id="m2",
                         original_size_bytes=5000, compressed_size_bytes=3000)

        photos_only = frozenset({".jpg"})
        stats = store.get_stats(include_extensions=photos_only)
        assert stats.total_original_bytes == 1000
        assert stats.total_compressed_bytes == 400

    def test_extension_filter_case_insensitive(self, store: StateStore):
        store.upsert_file(_make_record("/photos/A.JPG"))
        store.upsert_file(_make_record("/photos/b.Jpg"))
        store.upsert_file(_make_record("/videos/c.MP4"))

        photos_only = frozenset({".jpg"})
        pending = store.get_pending_files(include_extensions=photos_only)
        assert len(pending) == 2  # both .JPG and .Jpg should match .jpg


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Meta key/value store
# ---------------------------------------------------------------------------

class TestMeta:
    def test_get_missing_key_returns_none(self, store: StateStore):
        assert store.get_meta("album_id") is None

    def test_set_and_get_roundtrip(self, store: StateStore):
        store.set_meta("album_id", "album-abc-123")
        assert store.get_meta("album_id") == "album-abc-123"

    def test_set_overwrites_existing_value(self, store: StateStore):
        store.set_meta("album_id", "first")
        store.set_meta("album_id", "second")
        assert store.get_meta("album_id") == "second"

    def test_clear_all_removes_meta(self, store: StateStore):
        store.set_meta("album_id", "album-abc-123")
        store.clear_all()
        assert store.get_meta("album_id") is None

    def test_multiple_keys_independent(self, store: StateStore):
        store.set_meta("album_id", "abc")
        store.set_meta("other_key", "xyz")
        assert store.get_meta("album_id") == "abc"
        assert store.get_meta("other_key") == "xyz"


class TestThreadSafety:
    def test_concurrent_writes_do_not_corrupt(self, store: StateStore):
        """Multiple threads upsert distinct paths — final count must be exact."""
        n_threads = 8
        n_per_thread = 50

        def worker(thread_id: int):
            for i in range(n_per_thread):
                path = f"/photos/thread_{thread_id}/img_{i}.jpg"
                store.upsert_file(_make_record(path))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = store.get_stats()
        assert stats.total == n_threads * n_per_thread
