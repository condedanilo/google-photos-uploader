"""Unit tests for uploader/hasher.py."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from uploader.errors import FileReadError
from uploader.hasher import hash_file, hash_file_safe


class TestHashFile:
    def test_known_digest(self, tmp_path: Path):
        data = b"hello world"
        p = tmp_path / "test.bin"
        p.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert hash_file(p) == expected

    def test_empty_file(self, tmp_path: Path):
        p = tmp_path / "empty.bin"
        p.write_bytes(b"")
        expected = hashlib.sha256(b"").hexdigest()
        assert hash_file(p) == expected

    def test_large_file_streaming(self, tmp_path: Path):
        """Hash a file larger than a single chunk to verify streaming works."""
        data = b"x" * (200 * 1024)  # 200 KB — larger than _CHUNK_SIZE
        p = tmp_path / "big.bin"
        p.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        assert hash_file(p) == expected

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileReadError):
            hash_file(tmp_path / "nonexistent.jpg")

    def test_returns_hex_string(self, tmp_path: Path):
        p = tmp_path / "f.bin"
        p.write_bytes(b"abc")
        digest = hash_file(p)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_different_content_different_hash(self, tmp_path: Path):
        p1 = tmp_path / "a.bin"
        p2 = tmp_path / "b.bin"
        p1.write_bytes(b"content A")
        p2.write_bytes(b"content B")
        assert hash_file(p1) != hash_file(p2)

    def test_same_content_same_hash(self, tmp_path: Path):
        p1 = tmp_path / "a.bin"
        p2 = tmp_path / "b.bin"
        p1.write_bytes(b"identical")
        p2.write_bytes(b"identical")
        assert hash_file(p1) == hash_file(p2)


class TestHashFileSafe:
    def test_success_returns_digest_and_none(self, tmp_path: Path):
        p = tmp_path / "f.bin"
        p.write_bytes(b"safe")
        digest, exc = hash_file_safe(p)
        assert digest is not None
        assert exc is None

    def test_failure_returns_none_and_exception(self, tmp_path: Path):
        digest, exc = hash_file_safe(tmp_path / "missing.jpg")
        assert digest is None
        assert isinstance(exc, FileReadError)
