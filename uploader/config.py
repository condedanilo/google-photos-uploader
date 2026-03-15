"""Configuration loading with a clear precedence chain.

Precedence (highest → lowest):
  1. CLI flags (passed as keyword arguments to load())
  2. Environment variables  (GPHOTOS_*)
  3. uploader.toml in the current working directory
  4. ~/.config/gphotos-uploader/uploader.toml
  5. Built-in defaults
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from uploader.errors import ConfigError
from uploader.models import PHOTO_EXTENSIONS, VIDEO_EXTENSIONS, CompressionLevel


# ---------------------------------------------------------------------------
# AppConfig — immutable, fully-resolved configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AppConfig:
    # Paths
    credentials_path: Path   # client_secret.json from Google Cloud Console
    token_path: Path         # persisted OAuth token
    state_db_path: Path      # SQLite state database

    # Upload behaviour
    workers: int             # parallel byte-upload threads
    batch_size: int          # items per batchCreate call (API max: 50)
    max_retries: int         # per-file retry attempts before marking ERROR
    retry_base_delay: float  # seconds; formula: base * 2^attempt + jitter

    # Error handling
    on_quota_exhausted: str  # "exit" (v1 only)

    # Image compression
    compress: bool
    compression_level: CompressionLevel
    skip_if_larger: bool     # use original if compressed file is bigger

    # Video compression
    compress_video: bool
    video_max_height: int    # downscale if taller than this; 0 = no scaling
    video_crf: int           # H.264 CRF quality (18=high, 28=small, 23=default)
    video_preset: str        # ffmpeg speed/compression preset
    video_audio_bitrate: str # AAC audio bitrate (e.g. "128k")

    # Notifications
    notify_on_complete: bool
    beep_on_complete: bool

    # Scan
    follow_symlinks: bool
    include_extensions: Optional[frozenset[str]]  # None = all supported types

    # Album
    album: Optional[str]  # None = upload to library root (no album)
    album_per_dir: bool          # Create one album per subdirectory
    album_prefix: Optional[str]  # Optional prefix prepended to every album name


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

# Standard user-level config location. Exposed publicly so CLI commands can
# display it without duplicating the path.
USER_CONFIG_PATH = Path.home() / ".config" / "gphotos-uploader" / "uploader.toml"

_DEFAULT_CONFIG_PATHS = [
    Path.cwd() / "uploader.toml",
    USER_CONFIG_PATH,
]

# Content written to USER_CONFIG_PATH on first run. Mirrors uploader.example.toml
# but is embedded here so it works after `pip install` (no repo checkout needed).
_DEFAULT_CONFIG_CONTENT = """\
# Google Photos Uploader — Configuration File
# Edit this file to customise the tool's behaviour.
# CLI flags always override values set here.
#
# Discovery order (highest priority first):
#   1. CLI flags
#   2. Environment variables (GPHOTOS_*)
#   3. uploader.toml in the current directory
#   4. ~/.config/gphotos-uploader/uploader.toml  ← this file
#   5. Built-in defaults

[paths]
# Path to the OAuth client_secret.json downloaded from Google Cloud Console.
credentials = "~/.config/gphotos-uploader/client_secret.json"

# Where to persist the OAuth access token (created automatically after first login).
token = "~/.config/gphotos-uploader/token.json"

# Where to store the SQLite upload state database (created automatically).
state_db = "~/.local/share/gphotos-uploader/state.db"

[upload]
# Number of parallel worker threads for byte uploads.
workers = 4

# Maximum items per batchCreate call. The API hard limit is 50.
batch_size = 50

# Maximum retry attempts per file before marking it as an error.
max_retries = 3

# Base delay (seconds) for exponential backoff.
# Formula: base_delay * 2^attempt + random_jitter
retry_base_delay = 2.0

# Action when the daily API quota is exhausted.
on_quota_exhausted = "exit"

[compression]
# Enable local compression before upload. Reduces Google Photos storage usage.
enabled = true

# Compression aggressiveness:
#   "low"  — JPEG quality 92, ~30-40% smaller, virtually no visible difference
#   "mid"  — JPEG quality 85, Google "Storage Saver" equivalent (default)
#   "high" — JPEG quality 60, ~70-80% smaller, slight quality loss on close inspection
level = "mid"

# If the compressed file is larger than the original, upload the original instead.
skip_if_larger = true

[notifications]
# Emit an OS desktop notification when the upload run completes.
enabled = true

# Emit a terminal beep (ASCII bell) on completion.
beep = true

[scan]
# Follow symbolic links during directory scan.
follow_symlinks = false

# Restrict upload to specific extensions.
# Defaults to all Google Photos supported types when commented out.
# include_extensions = [".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov"]

[video_compression]
# Transcode videos to H.264/AAC before upload.
# Requires ffmpeg on PATH (brew install ffmpeg / sudo apt install ffmpeg).
enabled = true

# Downscale to this height if the video is taller. Set to 0 to skip scaling.
# Example: 1080 converts 4K → 1080p; 720 converts 1080p → 720p.
max_height = 1080

# H.264 quality (CRF). Lower = better quality and larger file.
#   18 = near-lossless, 23 = default, 28 = smaller/noticeable quality loss
crf = 23

# ffmpeg encoding speed preset. Slower presets = smaller files at same quality.
# Options: ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow
preset = "medium"

# AAC audio bitrate.
audio_bitrate = "128k"
"""

_DEFAULTS: dict = {
    "paths": {
        "credentials": str(Path.home() / ".config" / "gphotos-uploader" / "client_secret.json"),
        "token":       str(Path.home() / ".config" / "gphotos-uploader" / "token.json"),
        "state_db":    str(Path.home() / ".local" / "share" / "gphotos-uploader" / "state.db"),
    },
    "upload": {
        "workers":           4,
        "batch_size":        50,
        "max_retries":       3,
        "retry_base_delay":  2.0,
        "on_quota_exhausted": "exit",
    },
    "compression": {
        "enabled":       True,
        "level":         "mid",
        "skip_if_larger": True,
    },
    "video_compression": {
        "enabled":       True,
        "max_height":    1080,
        "crf":           23,
        "preset":        "medium",
        "audio_bitrate": "128k",
    },
    "notifications": {
        "enabled": True,
        "beep":    True,
    },
    "scan": {
        "follow_symlinks":    False,
        "include_extensions": None,
    },
}


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------

def load(
    config_file: Optional[Path] = None,
    *,
    # CLI overrides (None means "not set by user")
    workers: Optional[int] = None,
    compress: Optional[bool] = None,
    compression_level: Optional[str] = None,
    compress_video: Optional[bool] = None,
    max_retries: Optional[int] = None,
    credentials_path: Optional[Path] = None,
    token_path: Optional[Path] = None,
    state_db_path: Optional[Path] = None,
    album: Optional[str] = None,
    album_per_dir: bool = False,
    album_prefix: Optional[str] = None,
    media_type: Optional[str] = None,
) -> AppConfig:
    """Load and merge configuration from all sources, returning an AppConfig."""

    # 1. Load TOML file (first found wins)
    toml_data = _load_toml(config_file)

    # 2. Deep-merge with defaults (defaults are the base, toml overrides)
    merged = _deep_merge(_DEFAULTS, toml_data)

    # 3. Apply environment variable overrides
    merged = _apply_env(merged)

    # 4. Build the config object
    cfg = _build(merged)

    # 5. Apply CLI overrides (highest priority)
    overrides: dict = {}
    if workers is not None:
        overrides["workers"] = workers
    if compress is not None:
        overrides["compress"] = compress
    if compression_level is not None:
        overrides["compression_level"] = _parse_compression_level(compression_level)
    if compress_video is not None:
        overrides["compress_video"] = compress_video
    if max_retries is not None:
        overrides["max_retries"] = max_retries
    if credentials_path is not None:
        overrides["credentials_path"] = credentials_path
    if token_path is not None:
        overrides["token_path"] = token_path
    if state_db_path is not None:
        overrides["state_db_path"] = state_db_path
    if album is not None:
        overrides["album"] = album
    if album_per_dir:
        overrides["album_per_dir"] = True
    if album_prefix is not None:
        overrides["album_prefix"] = album_prefix
    if media_type is not None:
        _media_type_map = {
            "photos": PHOTO_EXTENSIONS,
            "videos": VIDEO_EXTENSIONS,
        }
        if media_type == "all":
            overrides["include_extensions"] = None
        elif media_type in _media_type_map:
            overrides["include_extensions"] = _media_type_map[media_type]
        else:
            raise ConfigError(
                f"Invalid media type '{media_type}'. Choose from: photos, videos, all"
            )

    if overrides:
        cfg = replace(cfg, **overrides)

    _validate(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_toml(explicit_path: Optional[Path]) -> dict:
    candidates = [explicit_path] if explicit_path else _DEFAULT_CONFIG_PATHS
    for path in candidates:
        if path and path.exists():
            try:
                with open(path, "rb") as f:
                    return tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f"Invalid TOML in {path}: {e}") from e
    return {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base (non-destructive copy)."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env(merged: dict) -> dict:
    """Apply GPHOTOS_* environment variable overrides."""
    env_map = {
        "GPHOTOS_CREDENTIALS": ("paths", "credentials"),
        "GPHOTOS_TOKEN":       ("paths", "token"),
        "GPHOTOS_STATE_DB":    ("paths", "state_db"),
        "GPHOTOS_WORKERS":     ("upload", "workers"),
        "GPHOTOS_MAX_RETRIES": ("upload", "max_retries"),
        "GPHOTOS_COMPRESS":    ("compression", "enabled"),
        "GPHOTOS_COMPRESSION_LEVEL": ("compression", "level"),
    }
    result = _deep_merge(merged, {})  # shallow copy at top level
    for env_var, (section, key) in env_map.items():
        value = os.environ.get(env_var)
        if value is not None:
            if section not in result:
                result[section] = {}
            # Type coercion for numeric/bool values
            if key in ("workers", "max_retries", "batch_size"):
                try:
                    result[section][key] = int(value)
                except ValueError:
                    raise ConfigError(f"Environment variable {env_var} must be an integer, got: {value!r}")
            elif key == "enabled":
                result[section][key] = value.lower() in ("1", "true", "yes")
            else:
                result[section][key] = value
    return result


def _build(d: dict) -> AppConfig:
    """Construct AppConfig from the merged dict, with type coercion."""
    p = d.get("paths", {})
    u = d.get("upload", {})
    c = d.get("compression", {})
    v = d.get("video_compression", {})
    n = d.get("notifications", {})
    s = d.get("scan", {})
    a = d.get("album", {})

    raw_extensions = s.get("include_extensions")
    include_extensions: Optional[frozenset[str]] = None
    if raw_extensions:
        include_extensions = frozenset(
            ext if ext.startswith(".") else f".{ext}"
            for ext in raw_extensions
        )

    return AppConfig(
        credentials_path   = _expand(p.get("credentials", _DEFAULTS["paths"]["credentials"])),
        token_path         = _expand(p.get("token", _DEFAULTS["paths"]["token"])),
        state_db_path      = _expand(p.get("state_db", _DEFAULTS["paths"]["state_db"])),

        workers            = int(u.get("workers", 4)),
        batch_size         = min(int(u.get("batch_size", 50)), 50),  # API hard limit
        max_retries        = int(u.get("max_retries", 3)),
        retry_base_delay   = float(u.get("retry_base_delay", 2.0)),
        on_quota_exhausted = str(u.get("on_quota_exhausted", "exit")),

        compress           = bool(c.get("enabled", True)),
        compression_level  = _parse_compression_level(c.get("level", "mid")),
        skip_if_larger     = bool(c.get("skip_if_larger", True)),

        compress_video     = bool(v.get("enabled", True)),
        video_max_height   = int(v.get("max_height", 1080)),
        video_crf          = int(v.get("crf", 23)),
        video_preset       = str(v.get("preset", "medium")),
        video_audio_bitrate = str(v.get("audio_bitrate", "128k")),

        notify_on_complete = bool(n.get("enabled", True)),
        beep_on_complete   = bool(n.get("beep", True)),

        follow_symlinks    = bool(s.get("follow_symlinks", False)),
        include_extensions = include_extensions,

        album              = a.get("name") or None,
        album_per_dir      = False,
        album_prefix       = None,
    )


def _parse_compression_level(value: str) -> CompressionLevel:
    try:
        return CompressionLevel(value.lower())
    except ValueError:
        valid = ", ".join(v.value for v in CompressionLevel)
        raise ConfigError(
            f"Invalid compression level {value!r}. Must be one of: {valid}"
        )


def _expand(path_str: str) -> Path:
    return Path(os.path.expandvars(path_str)).expanduser()


def ensure_user_config() -> Path | None:
    """Create the default user config at USER_CONFIG_PATH if it doesn't exist.

    Returns the path to the newly created file, or None if it already existed
    or a local uploader.toml in the current directory was found (in which case
    the user is intentionally using a local config and we leave it alone).
    """
    if USER_CONFIG_PATH.exists():
        return None
    if (Path.cwd() / "uploader.toml").exists():
        return None
    USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    USER_CONFIG_PATH.write_text(_DEFAULT_CONFIG_CONTENT, encoding="utf-8")
    return USER_CONFIG_PATH


def _validate(cfg: AppConfig) -> None:
    if cfg.workers < 1:
        raise ConfigError("workers must be at least 1")
    if cfg.max_retries < 0:
        raise ConfigError("max_retries must be >= 0")
    if cfg.batch_size < 1 or cfg.batch_size > 50:
        raise ConfigError("batch_size must be between 1 and 50")
    if cfg.retry_base_delay < 0:
        raise ConfigError("retry_base_delay must be >= 0")
    if cfg.on_quota_exhausted not in ("exit",):
        raise ConfigError(f"on_quota_exhausted must be 'exit', got: {cfg.on_quota_exhausted!r}")
    if cfg.album and cfg.album_per_dir:
        raise ConfigError("--album and --albums-from-dirs cannot be used together")
