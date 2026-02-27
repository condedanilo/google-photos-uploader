"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Image fixture factories — generated at test time, no binary blobs in git
# ---------------------------------------------------------------------------

def _make_jpeg(path: Path, width: int = 100, height: int = 100) -> Path:
    """Create a small valid JPEG using Pillow."""
    from PIL import Image
    img = Image.new("RGB", (width, height), color=(120, 80, 200))
    img.save(str(path), format="JPEG", quality=95)
    return path


def _make_png(path: Path, width: int = 100, height: int = 100) -> Path:
    from PIL import Image
    img = Image.new("RGB", (width, height), color=(40, 180, 90))
    img.save(str(path), format="PNG")
    return path


@pytest.fixture
def sample_jpeg(tmp_path: Path) -> Path:
    """Small valid JPEG file."""
    return _make_jpeg(tmp_path / "sample.jpg")


@pytest.fixture
def sample_png(tmp_path: Path) -> Path:
    """Small valid PNG file."""
    return _make_png(tmp_path / "sample.png")


@pytest.fixture
def large_jpeg(tmp_path: Path) -> Path:
    """A valid but larger JPEG (500x500) to test compression ratios."""
    return _make_jpeg(tmp_path / "large.jpg", width=500, height=500)


@pytest.fixture
def corrupt_jpeg(tmp_path: Path) -> Path:
    """A file with a .jpg extension but invalid content."""
    p = tmp_path / "corrupt.jpg"
    p.write_bytes(b"this is not a valid jpeg file \xff\xd8\x00\xde\xad\xbe\xef")
    return p


@pytest.fixture
def empty_file(tmp_path: Path) -> Path:
    """A 0-byte file."""
    p = tmp_path / "empty.jpg"
    p.write_bytes(b"")
    return p


@pytest.fixture
def photo_tree(tmp_path: Path) -> Path:
    """
    A directory tree with a mix of supported and unsupported files:
      photos/
        a/
          img1.jpg
          img2.png
          doc.txt        (unsupported — should be ignored)
        b/
          img3.jpg
          empty.jpg      (0 bytes — should be flagged)
        video.mp4        (1-byte placeholder — valid extension)
    Returns the root 'photos' directory.
    """
    root = tmp_path / "photos"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)

    _make_jpeg(root / "a" / "img1.jpg")
    _make_png(root / "a" / "img2.png")
    (root / "a" / "doc.txt").write_text("not a photo")

    _make_jpeg(root / "b" / "img3.jpg")
    (root / "b" / "empty.jpg").write_bytes(b"")

    # Minimal valid-looking MP4 (just needs a supported extension for scanner tests)
    (root / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftypisom")

    return root
