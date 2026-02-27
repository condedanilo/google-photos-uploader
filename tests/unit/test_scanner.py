"""Unit tests for uploader/scanner.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from uploader.scanner import scan_directory


class TestScanDirectory:
    def test_finds_supported_images(self, photo_tree: Path):
        result = scan_directory(photo_tree)
        names = {p.name for p in result.paths}
        assert "img1.jpg" in names
        assert "img2.png" in names
        assert "img3.jpg" in names
        assert "video.mp4" in names

    def test_ignores_unsupported_extension(self, photo_tree: Path):
        result = scan_directory(photo_tree)
        names = {p.name for p in result.paths}
        assert "doc.txt" not in names

    def test_empty_file_is_found(self, photo_tree: Path):
        # The scanner finds all extensions; 0-byte filtering is done later
        result = scan_directory(photo_tree)
        names = {p.name for p in result.paths}
        assert "empty.jpg" in names

    def test_skipped_count_increments_for_bad_extensions(self, photo_tree: Path):
        result = scan_directory(photo_tree)
        assert result.skipped_count >= 1   # at least doc.txt is skipped

    def test_include_extensions_filter(self, photo_tree: Path):
        result = scan_directory(photo_tree, include_extensions=frozenset({".jpg"}))
        exts = {p.suffix.lower() for p in result.paths}
        assert exts == {".jpg"}

    def test_recursive_discovery(self, photo_tree: Path):
        result = scan_directory(photo_tree)
        # Files in subdirectories a/ and b/ are found
        assert any(p.parent.name == "a" for p in result.paths)
        assert any(p.parent.name == "b" for p in result.paths)

    def test_on_file_found_callback(self, photo_tree: Path):
        counts: list[int] = []
        scan_directory(photo_tree, on_file_found=lambda n, p: counts.append(n))
        assert counts == list(range(1, len(counts) + 1))  # monotonically increasing
        assert len(counts) > 0

    def test_non_directory_raises(self, tmp_path: Path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        with pytest.raises(NotADirectoryError):
            scan_directory(f)

    def test_empty_directory(self, tmp_path: Path):
        result = scan_directory(tmp_path)
        assert result.paths == []
        assert result.skipped_count == 0

    def test_symlinks_ignored_by_default(self, tmp_path: Path):
        target = tmp_path / "real.jpg"
        target.write_bytes(b"data")
        link = tmp_path / "link.jpg"
        link.symlink_to(target)
        result = scan_directory(tmp_path, follow_symlinks=False)
        names = {p.name for p in result.paths}
        # Only the real file should appear, not the symlink
        assert "real.jpg" in names
        # Both may appear as separate paths, but the link should be excluded
        assert not any(p.is_symlink() for p in result.paths)

    def test_case_insensitive_extension(self, tmp_path: Path):
        (tmp_path / "IMG.JPG").write_bytes(b"x")
        (tmp_path / "img.JPEG").write_bytes(b"x")
        result = scan_directory(tmp_path)
        assert len(result.paths) == 2
