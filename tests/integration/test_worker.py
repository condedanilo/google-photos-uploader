"""Integration tests for uploader/worker.py.

Uses real SQLite (in a temp directory) and mocked HTTP via respx.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from uploader.api_client import GooglePhotosClient, _UPLOAD_URL
from uploader.config import AppConfig
from uploader.models import CompressionLevel, FileRecord, FileStatus
from uploader.state import StateStore
from uploader.worker import upload_file


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture
def fake_creds():
    creds = MagicMock()
    creds.token = "fake-token"
    creds.expired = False
    return creds


@pytest.fixture
def client(fake_creds) -> GooglePhotosClient:
    return GooglePhotosClient(fake_creds, timeout=5.0)


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    from uploader.config import _DEFAULTS
    from dataclasses import replace
    from uploader.config import _build, _deep_merge
    cfg = _build(_deep_merge(_DEFAULTS, {}))
    # Disable compression for simplicity in integration tests
    return replace(cfg,
                   compress=False,
                   workers=2,
                   max_retries=2,
                   retry_base_delay=0.0)


def _make_token_queue():
    return queue.Queue()


def _make_events():
    shutdown = threading.Event()
    progress = threading.Event()
    return shutdown, progress


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestUploadFileWorker:
    @respx.mock
    def test_success_puts_token_on_queue(self, store: StateStore, client, config, sample_jpeg):
        respx.post(_UPLOAD_URL).mock(return_value=httpx.Response(200, text="test-upload-token"))

        rec = FileRecord(path=str(sample_jpeg))
        fid = store.upsert_file(rec)
        rec = FileRecord(id=fid, path=str(sample_jpeg))

        token_q = _make_token_queue()
        shutdown, progress = _make_events()

        upload_file(rec, config, client, store, token_q, shutdown, progress)

        assert not token_q.empty()
        token = token_q.get_nowait()
        assert token.token == "test-upload-token"
        assert token.file_id == fid

    @respx.mock
    def test_empty_file_set_to_skipped(self, store: StateStore, client, config, empty_file):
        rec = FileRecord(path=str(empty_file))
        fid = store.upsert_file(rec)
        rec = FileRecord(id=fid, path=str(empty_file))

        token_q = _make_token_queue()
        shutdown, progress = _make_events()

        upload_file(rec, config, client, store, token_q, shutdown, progress)

        assert token_q.empty()
        errors = store.get_files_by_status(FileStatus.ERROR)
        assert len(errors) == 1
        assert "empty" in errors[0].error_msg.lower()

    @respx.mock
    def test_missing_file_set_to_error(self, store: StateStore, client, config, tmp_path):
        missing = tmp_path / "nonexistent.jpg"
        rec = FileRecord(path=str(missing))
        fid = store.upsert_file(rec)
        rec = FileRecord(id=fid, path=str(missing))

        token_q = _make_token_queue()
        shutdown, progress = _make_events()

        upload_file(rec, config, client, store, token_q, shutdown, progress)

        assert token_q.empty()
        errors = store.get_files_by_status(FileStatus.ERROR)
        assert errors[0].error_msg is not None

    @respx.mock
    def test_shutdown_event_skips_processing(self, store: StateStore, client, config, sample_jpeg):
        rec = FileRecord(path=str(sample_jpeg))
        fid = store.upsert_file(rec)
        rec = FileRecord(id=fid, path=str(sample_jpeg))

        token_q = _make_token_queue()
        shutdown, progress = _make_events()
        shutdown.set()  # set before calling

        upload_file(rec, config, client, store, token_q, shutdown, progress)

        assert token_q.empty()
        # Status should still be PENDING (never reached IN_PROGRESS)
        pending = store.get_pending_files()
        assert len(pending) == 1

    @respx.mock
    def test_sizes_recorded_in_state(self, store: StateStore, client, config, large_jpeg):
        respx.post(_UPLOAD_URL).mock(return_value=httpx.Response(200, text="tok"))

        rec = FileRecord(path=str(large_jpeg))
        fid = store.upsert_file(rec)
        rec = FileRecord(id=fid, path=str(large_jpeg))

        token_q = _make_token_queue()
        shutdown, progress = _make_events()

        upload_file(rec, config, client, store, token_q, shutdown, progress)

        rows = store.get_files_by_status(FileStatus.IN_PROGRESS)
        assert len(rows) == 1
        assert rows[0].original_size_bytes is not None
        assert rows[0].original_size_bytes > 0

    @respx.mock
    def test_deduplication_skips_already_uploaded(self, store: StateStore, client, config, sample_jpeg):
        # Register the same file as already uploaded
        from uploader.hasher import hash_file
        h = hash_file(sample_jpeg)

        uploaded = FileRecord(path="/other/path.jpg", status=FileStatus.UPLOADED, content_hash=h)
        store.upsert_file(uploaded)

        rec = FileRecord(path=str(sample_jpeg))
        fid = store.upsert_file(rec)
        rec = FileRecord(id=fid, path=str(sample_jpeg))

        token_q = _make_token_queue()
        shutdown, progress = _make_events()

        upload_file(rec, config, client, store, token_q, shutdown, progress)

        assert token_q.empty()
        skipped = store.get_files_by_status(FileStatus.SKIPPED)
        assert any(str(sample_jpeg) in s.path for s in skipped)
