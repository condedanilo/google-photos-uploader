"""Integration tests for uploader/orchestrator.py.

Uses real SQLite and mocked HTTP. No actual Google API calls are made.
"""
from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from uploader.api_client import _BATCH_CREATE_URL, _UPLOAD_URL
from uploader.models import FileRecord, FileStatus, RunStats
from uploader.orchestrator import run_upload, scan_and_register
from uploader.state import StateStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    s = StateStore(tmp_path / "state.db")
    yield s
    s.close()


@pytest.fixture
def config(tmp_path: Path):
    from uploader.config import _build, _deep_merge, _DEFAULTS
    cfg = _build(_deep_merge(_DEFAULTS, {}))
    return replace(cfg, compress=False, compress_video=False, workers=2, max_retries=1, retry_base_delay=0.0)


@pytest.fixture
def fake_creds():
    creds = MagicMock()
    creds.token = "fake-token"
    creds.expired = False
    return creds


def _batch_ok(file_ids: list[int]) -> dict:
    return {
        "newMediaItemResults": [
            {"status": {"code": 0}, "mediaItem": {"id": f"media-{fid}"}}
            for fid in file_ids
        ]
    }


def _register_files(store: StateStore, paths: list[Path]) -> None:
    """Pre-populate state with PENDING records."""
    for p in paths:
        store.upsert_file(FileRecord(
            path=str(p),
            status=FileStatus.PENDING,
            content_hash=f"hash-{p.name}",
            original_size_bytes=p.stat().st_size,
        ))


# ---------------------------------------------------------------------------
# scan_and_register tests
# ---------------------------------------------------------------------------

class TestScanAndRegister:
    def test_registers_supported_files(self, store, config, photo_tree):
        count = scan_and_register(photo_tree, config, store, lambda n, p: None)
        stats = store.get_stats()
        # img1.jpg, img2.png, img3.jpg, video.mp4 = 4 supported files
        # empty.jpg = 0 bytes → SKIPPED
        assert stats.total >= 4
        pending = store.get_pending_files()
        assert len(pending) >= 3   # non-empty images and video

    def test_empty_files_marked_skipped(self, store, config, photo_tree):
        scan_and_register(photo_tree, config, store, lambda n, p: None)
        skipped = store.get_files_by_status(FileStatus.SKIPPED)
        skipped_names = [Path(s.path).name for s in skipped]
        assert "empty.jpg" in skipped_names

    def test_duplicate_hash_marked_skipped(self, store, config, tmp_path):
        # Create two identical files
        img1 = tmp_path / "img1.jpg"
        img2 = tmp_path / "img2.jpg"
        from tests.conftest import _make_jpeg
        _make_jpeg(img1)
        import shutil
        shutil.copy(img1, img2)

        # Mark img1 as already uploaded
        from uploader.hasher import hash_file
        h = hash_file(img1)
        store.upsert_file(FileRecord(path=str(img1), status=FileStatus.UPLOADED, content_hash=h))

        # scan img2 (same hash)
        scan_and_register(tmp_path, config, store, lambda n, p: None)
        skipped = store.get_files_by_status(FileStatus.SKIPPED)
        assert any("img2.jpg" in s.path for s in skipped)


# ---------------------------------------------------------------------------
# run_upload tests
# ---------------------------------------------------------------------------

class TestRunUpload:
    @respx.mock
    def test_all_files_uploaded(self, store, config, tmp_path, fake_creds):
        # Create 3 small JPEG files
        from tests.conftest import _make_jpeg
        paths = [_make_jpeg(tmp_path / f"img{i}.jpg") for i in range(3)]
        _register_files(store, paths)

        respx.post(_UPLOAD_URL).mock(return_value=httpx.Response(200, text="upload-tok"))
        respx.post(_BATCH_CREATE_URL).mock(
            return_value=httpx.Response(200, json=_batch_ok([1, 2, 3]))
        )

        stats_calls: list[RunStats] = []
        shutdown = threading.Event()

        with patch("uploader.orchestrator.get_credentials", return_value=fake_creds):
            stats = run_upload(config, store, stats_calls.append, shutdown)

        assert stats.errors == 0
        assert len(stats_calls) > 0

    @respx.mock
    def test_no_pending_files_returns_empty_stats(self, store, config, fake_creds):
        shutdown = threading.Event()
        with patch("uploader.orchestrator.get_credentials", return_value=fake_creds):
            stats = run_upload(config, store, lambda s: None, shutdown)
        assert stats.total == 0

    @respx.mock
    def test_shutdown_mid_run_leaves_pending(self, store, config, tmp_path, fake_creds):
        from tests.conftest import _make_jpeg

        # Create 4 files
        paths = [_make_jpeg(tmp_path / f"img{i}.jpg") for i in range(4)]
        _register_files(store, paths)

        shutdown = threading.Event()

        call_count = [0]

        def upload_side_effect(request):
            call_count[0] += 1
            if call_count[0] == 2:
                shutdown.set()  # trigger shutdown after 2nd upload
            return httpx.Response(200, text="tok")

        respx.post(_UPLOAD_URL).mock(side_effect=upload_side_effect)
        respx.post(_BATCH_CREATE_URL).mock(
            return_value=httpx.Response(200, json={"newMediaItemResults": [
                {"status": {"code": 0}, "mediaItem": {"id": "m1"}},
                {"status": {"code": 0}, "mediaItem": {"id": "m2"}},
            ]})
        )

        with patch("uploader.orchestrator.get_credentials", return_value=fake_creds):
            stats = run_upload(config, store, lambda s: None, shutdown)

        # Not all files should be uploaded — some remain pending
        final_stats = store.get_stats()
        assert final_stats.uploaded < 4
