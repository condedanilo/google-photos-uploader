"""Unit tests for uploader/compressor.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from uploader.compressor import cleanup_temp, compress
from uploader.errors import CompressionError
from uploader.models import CompressionLevel


class TestCompressImages:
    def test_jpeg_is_compressed(self, large_jpeg: Path, tmp_path: Path):
        result = compress(large_jpeg, CompressionLevel.MID, _tmp_dir=tmp_path)
        # A 500x500 JPEG saved at q=95 should compress smaller at q=85
        assert result.was_compressed
        assert result.output_bytes < result.original_bytes
        assert Path(result.output_path).exists()

    def test_output_is_valid_jpeg(self, large_jpeg: Path, tmp_path: Path):
        from PIL import Image
        result = compress(large_jpeg, CompressionLevel.HIGH, _tmp_dir=tmp_path)
        img = Image.open(result.output_path)
        assert img.format == "JPEG"

    def test_png_converted_to_jpeg(self, sample_png: Path, tmp_path: Path):
        from PIL import Image
        result = compress(sample_png, CompressionLevel.MID, _tmp_dir=tmp_path)
        if result.was_compressed:
            img = Image.open(result.output_path)
            assert img.format == "JPEG"

    def test_skip_if_larger_uses_original(self, sample_jpeg: Path, tmp_path: Path):
        """A tiny JPEG may not shrink further — original should be used."""
        # We can't guarantee compression always reduces size, but skip_if_larger=True
        # must never return a file larger than the original
        result = compress(sample_jpeg, CompressionLevel.LOW, skip_if_larger=True, _tmp_dir=tmp_path)
        assert result.output_bytes <= result.original_bytes

    def test_skip_if_larger_false_keeps_compressed(self, sample_jpeg: Path, tmp_path: Path):
        result = compress(sample_jpeg, CompressionLevel.LOW, skip_if_larger=False, _tmp_dir=tmp_path)
        # Result exists regardless of size comparison
        assert result.output_path is not None

    def test_video_passthrough(self, tmp_path: Path):
        mp4 = tmp_path / "video.mp4"
        mp4.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 100)
        result = compress(mp4, CompressionLevel.MID, _tmp_dir=tmp_path)
        assert not result.was_compressed
        assert result.output_path == str(mp4)
        assert result.output_bytes == result.original_bytes

    def test_corrupt_image_raises_compression_error(self, corrupt_jpeg: Path, tmp_path: Path):
        with pytest.raises(CompressionError):
            compress(corrupt_jpeg, CompressionLevel.MID, _tmp_dir=tmp_path)

    def test_compression_result_bytes_saved(self, large_jpeg: Path, tmp_path: Path):
        result = compress(large_jpeg, CompressionLevel.HIGH, _tmp_dir=tmp_path)
        if result.was_compressed:
            assert result.bytes_saved >= 0
            assert 0.0 <= result.ratio <= 1.0

    def test_temp_file_in_specified_dir(self, large_jpeg: Path, tmp_path: Path):
        result = compress(large_jpeg, CompressionLevel.MID, _tmp_dir=tmp_path)
        if result.was_compressed:
            assert Path(result.output_path).parent == tmp_path


class TestCleanupTemp:
    def test_deletes_temp_file(self, large_jpeg: Path, tmp_path: Path):
        result = compress(large_jpeg, CompressionLevel.HIGH, _tmp_dir=tmp_path)
        if result.was_compressed:
            out = Path(result.output_path)
            assert out.exists()
            cleanup_temp(result)
            assert not out.exists()

    def test_passthrough_cleanup_is_noop(self, tmp_path: Path):
        mp4 = tmp_path / "video.mp4"
        mp4.write_bytes(b"data")
        result = compress(mp4, CompressionLevel.MID, _tmp_dir=tmp_path)
        cleanup_temp(result)  # should not raise or delete the original
        assert mp4.exists()
