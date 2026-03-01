"""Local image and video compression before upload.

Design:
  - JPEG/PNG/HEIC → compressed JPEG written to a temp file
  - EXIF DateTimeOriginal + GPS tags are preserved so Google Photos uses
    the original capture date instead of the upload date
  - If the compressed output is larger than the original, the original is used
  - Video files → transcoded to H.264/AAC via ffmpeg (requires ffmpeg on PATH)
  - The caller is responsible for deleting the temp file after use
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from uploader.errors import CompressionError, FfmpegNotFoundError
from uploader.models import (
    COMPRESSIBLE_EXTENSIONS,
    CompressionLevel,
    CompressionResult,
)

# Pillow and piexif are imported lazily to keep startup fast
# and to allow the module to be imported on systems where they are not yet installed.


def compress(
    source: Path,
    level: CompressionLevel,
    *,
    skip_if_larger: bool = True,
    _tmp_dir: Optional[Path] = None,
) -> CompressionResult:
    """Compress *source* and write the result to a temp file.

    Args:
        source: The original image file.
        level: Compression preset (LOW, MID, HIGH).
        skip_if_larger: If True and the compressed file is larger, use the original.
        _tmp_dir: Override the temp directory (used in tests).

    Returns:
        CompressionResult with paths and size information.

    Raises:
        CompressionError: if the image cannot be opened or saved.
    """
    ext = source.suffix.lower()
    original_bytes = source.stat().st_size

    # Non-compressible types: return the original unchanged
    if ext not in COMPRESSIBLE_EXTENSIONS:
        return CompressionResult(
            source_path=str(source),
            output_path=str(source),
            original_bytes=original_bytes,
            output_bytes=original_bytes,
            was_compressed=False,
        )

    quality = level.jpeg_quality()
    tmp_path = _make_temp_path(_tmp_dir)

    try:
        _compress_image(source, tmp_path, quality=quality)
    except Exception as e:
        _safe_delete(tmp_path)
        raise CompressionError(f"Could not compress {source}: {e}") from e

    compressed_bytes = tmp_path.stat().st_size

    # If compression made the file larger, fall back to the original
    if skip_if_larger and compressed_bytes >= original_bytes:
        _safe_delete(tmp_path)
        return CompressionResult(
            source_path=str(source),
            output_path=str(source),
            original_bytes=original_bytes,
            output_bytes=original_bytes,
            was_compressed=False,
        )

    return CompressionResult(
        source_path=str(source),
        output_path=str(tmp_path),
        original_bytes=original_bytes,
        output_bytes=compressed_bytes,
        was_compressed=True,
    )


def cleanup_temp(result: CompressionResult) -> None:
    """Delete the temp compressed file if one was created."""
    if result.was_compressed and result.output_path != result.source_path:
        _safe_delete(Path(result.output_path))


# ---------------------------------------------------------------------------
# Video compression
# ---------------------------------------------------------------------------

def check_ffmpeg() -> None:
    """Raise FfmpegNotFoundError if ffmpeg is not available on PATH."""
    if shutil.which("ffmpeg") is None:
        raise FfmpegNotFoundError()


def compress_video(
    source: Path,
    *,
    max_height: int = 1080,
    crf: int = 23,
    preset: str = "medium",
    audio_bitrate: str = "128k",
    skip_if_larger: bool = True,
    _tmp_dir: Optional[Path] = None,
) -> CompressionResult:
    """Transcode *source* video to H.264/AAC MP4 and write to a temp file.

    Args:
        source: The original video file.
        max_height: Downscale if video height exceeds this value. 0 = no scaling.
        crf: H.264 CRF quality (lower = better quality, larger file).
        preset: ffmpeg speed/compression preset.
        audio_bitrate: AAC audio bitrate (e.g. "128k").
        skip_if_larger: If True and the transcoded file is larger, use the original.
        _tmp_dir: Override the temp directory (used in tests).

    Returns:
        CompressionResult with paths and size information.

    Raises:
        CompressionError: if ffprobe or ffmpeg fail.
        FfmpegNotFoundError: if ffmpeg is not on PATH.
    """
    original_bytes = source.stat().st_size

    # Determine video height via ffprobe
    height = _probe_video_height(source)

    # Build ffmpeg command
    cmd = ["ffmpeg", "-i", str(source), "-c:v", "libx264", "-crf", str(crf),
           "-preset", preset, "-c:a", "aac", "-b:a", audio_bitrate,
           "-movflags", "+faststart"]

    if max_height > 0 and height is not None and height > max_height:
        # scale=-2:max_height keeps aspect ratio and ensures even width
        cmd += ["-vf", f"scale=-2:{max_height}"]

    tmp_path = _make_temp_video_path(_tmp_dir)
    cmd += ["-y", str(tmp_path)]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=3600,  # 1 hour max per video
        )
    except subprocess.TimeoutExpired as e:
        _safe_delete(tmp_path)
        raise CompressionError(f"ffmpeg timed out on {source}") from e
    except OSError as e:
        _safe_delete(tmp_path)
        raise FfmpegNotFoundError() from e

    if result.returncode != 0:
        _safe_delete(tmp_path)
        stderr = result.stderr.decode(errors="replace").strip()
        raise CompressionError(f"ffmpeg failed on {source}: {stderr[-300:]}")

    transcoded_bytes = tmp_path.stat().st_size

    if skip_if_larger and transcoded_bytes >= original_bytes:
        _safe_delete(tmp_path)
        return CompressionResult(
            source_path=str(source),
            output_path=str(source),
            original_bytes=original_bytes,
            output_bytes=original_bytes,
            was_compressed=False,
        )

    return CompressionResult(
        source_path=str(source),
        output_path=str(tmp_path),
        original_bytes=original_bytes,
        output_bytes=transcoded_bytes,
        was_compressed=True,
    )


def _probe_video_height(source: Path) -> Optional[int]:
    """Return the video stream height in pixels, or None if ffprobe fails."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=height",
                "-of", "csv=p=0",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        line = result.stdout.strip()
        return int(line) if line.isdigit() else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compress_image(source: Path, dest: Path, quality: int) -> None:
    """Open *source*, compress to JPEG at *quality*, save to *dest*.

    Preserves DateTimeOriginal and GPS EXIF tags so Google Photos can
    correctly date and locate the photo.
    """
    from PIL import Image  # type: ignore
    import piexif           # type: ignore

    # Register pillow-heif for HEIC/HEIF support (no-op if not installed)
    try:
        from pillow_heif import register_heif_opener  # type: ignore
        register_heif_opener()
    except ImportError:
        pass

    try:
        img = Image.open(str(source))
    except Exception as e:
        raise CompressionError(f"Cannot open image: {e}") from e

    # Convert to RGB (handles RGBA, P, CMYK, etc.)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    # Extract and filter EXIF
    exif_bytes = _extract_filtered_exif(img, source)

    save_kwargs: dict = {
        "format": "JPEG",
        "quality": quality,
        "optimize": True,
        "progressive": True,
    }
    if exif_bytes:
        save_kwargs["exif"] = exif_bytes

    try:
        img.save(str(dest), **save_kwargs)
    except Exception as e:
        raise CompressionError(f"Cannot save compressed image: {e}") from e


def _extract_filtered_exif(img, source: Path) -> Optional[bytes]:
    """Return EXIF bytes with only date/time and GPS tags preserved.

    We deliberately strip MakerNote, Thumbnail, and software-specific tags
    to reduce file size while keeping the metadata Google Photos needs.
    """
    try:
        import piexif  # type: ignore
    except ImportError:
        return None

    # Try to get EXIF from the PIL image first
    raw_exif: Optional[bytes] = None
    try:
        raw_exif = img.info.get("exif")
    except Exception:
        pass

    if not raw_exif:
        return None

    try:
        exif_dict = piexif.load(raw_exif)
    except Exception:
        return None

    # Tags to keep in the Exif IFD (0th and Exif sub-IFD)
    _KEEP_EXIF_TAGS = {
        piexif.ExifIFD.DateTimeOriginal,
        piexif.ExifIFD.DateTimeDigitized,
        piexif.ExifIFD.OffsetTimeOriginal,
        piexif.ExifIFD.OffsetTimeDigitized,
    }
    _KEEP_0TH_TAGS = {
        piexif.ImageIFD.DateTime,
        piexif.ImageIFD.Make,
        piexif.ImageIFD.Model,
        piexif.ImageIFD.Orientation,
        piexif.ImageIFD.XResolution,
        piexif.ImageIFD.YResolution,
        piexif.ImageIFD.ResolutionUnit,
    }

    filtered: dict = {
        "0th":  {k: v for k, v in exif_dict.get("0th", {}).items()  if k in _KEEP_0TH_TAGS},
        "Exif": {k: v for k, v in exif_dict.get("Exif", {}).items() if k in _KEEP_EXIF_TAGS},
        "GPS":  exif_dict.get("GPS", {}),    # keep all GPS tags
        "1st":  {},                           # strip thumbnail
        "Interop": {},
    }

    try:
        return piexif.dump(filtered)
    except Exception:
        return None


def _make_temp_path(tmp_dir: Optional[Path]) -> Path:
    kwargs: dict = {"suffix": ".jpg"}
    if tmp_dir:
        kwargs["dir"] = str(tmp_dir)
    fd, name = tempfile.mkstemp(**kwargs)
    os.close(fd)
    return Path(name)


def _make_temp_video_path(tmp_dir: Optional[Path]) -> Path:
    kwargs: dict = {"suffix": ".mp4"}
    if tmp_dir:
        kwargs["dir"] = str(tmp_dir)
    fd, name = tempfile.mkstemp(**kwargs)
    os.close(fd)
    return Path(name)


def _safe_delete(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
