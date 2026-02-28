"""Unit tests for uploader/config.py."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from uploader.config import (
    USER_CONFIG_PATH,
    _DEFAULT_CONFIG_CONTENT,
    ensure_user_config,
    load,
)
from uploader.errors import ConfigError
from uploader.models import CompressionLevel


# ---------------------------------------------------------------------------
# ensure_user_config
# ---------------------------------------------------------------------------

class TestEnsureUserConfig:
    def test_creates_file_when_missing(self, tmp_path, monkeypatch):
        user_cfg = tmp_path / ".config" / "gphotos-uploader" / "uploader.toml"
        monkeypatch.setattr("uploader.config.USER_CONFIG_PATH", user_cfg)
        # Ensure cwd has no uploader.toml
        monkeypatch.chdir(tmp_path)

        result = ensure_user_config()

        assert result == user_cfg
        assert user_cfg.exists()
        content = user_cfg.read_text()
        assert "[upload]" in content
        assert "[compression]" in content

    def test_returns_none_when_file_already_exists(self, tmp_path, monkeypatch):
        user_cfg = tmp_path / ".config" / "gphotos-uploader" / "uploader.toml"
        user_cfg.parent.mkdir(parents=True)
        user_cfg.write_text("[upload]\nworkers = 4\n")
        monkeypatch.setattr("uploader.config.USER_CONFIG_PATH", user_cfg)
        monkeypatch.chdir(tmp_path)

        result = ensure_user_config()

        assert result is None

    def test_returns_none_when_local_config_exists(self, tmp_path, monkeypatch):
        user_cfg = tmp_path / ".config" / "gphotos-uploader" / "uploader.toml"
        monkeypatch.setattr("uploader.config.USER_CONFIG_PATH", user_cfg)
        # Create a local uploader.toml in cwd
        local_cfg = tmp_path / "uploader.toml"
        local_cfg.write_text("[upload]\nworkers = 2\n")
        monkeypatch.chdir(tmp_path)

        result = ensure_user_config()

        assert result is None
        assert not user_cfg.exists()

    def test_creates_parent_directories(self, tmp_path, monkeypatch):
        deep_path = tmp_path / "a" / "b" / "c" / "uploader.toml"
        monkeypatch.setattr("uploader.config.USER_CONFIG_PATH", deep_path)
        monkeypatch.chdir(tmp_path)

        result = ensure_user_config()

        assert result == deep_path
        assert deep_path.exists()

    def test_default_content_is_valid_toml(self, tmp_path, monkeypatch):
        import tomllib
        user_cfg = tmp_path / "uploader.toml"
        monkeypatch.setattr("uploader.config.USER_CONFIG_PATH", user_cfg)
        # Use a subdirectory with no uploader.toml so the cwd check doesn't trigger
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        monkeypatch.chdir(subdir)

        # Write and parse to verify valid TOML
        ensure_user_config()
        with open(user_cfg, "rb") as f:
            data = tomllib.load(f)

        assert "upload" in data
        assert "compression" in data
        assert "paths" in data


# ---------------------------------------------------------------------------
# load() — basic behaviour
# ---------------------------------------------------------------------------

class TestLoad:
    def test_returns_defaults_when_no_config_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("uploader.config.USER_CONFIG_PATH", tmp_path / "none.toml")
        monkeypatch.chdir(tmp_path)

        cfg = load()

        assert cfg.workers == 4
        assert cfg.compress is True
        assert cfg.compression_level == CompressionLevel.MID
        assert cfg.max_retries == 3

    def test_loads_explicit_config_file(self, tmp_path):
        cfg_file = tmp_path / "custom.toml"
        cfg_file.write_text("[upload]\nworkers = 8\n")

        cfg = load(config_file=cfg_file)

        assert cfg.workers == 8

    def test_cli_override_wins_over_file(self, tmp_path):
        cfg_file = tmp_path / "custom.toml"
        cfg_file.write_text("[upload]\nworkers = 8\n")

        cfg = load(config_file=cfg_file, workers=2)

        assert cfg.workers == 2

    def test_raises_on_invalid_toml(self, tmp_path):
        cfg_file = tmp_path / "bad.toml"
        cfg_file.write_text("this is not toml ][")

        with pytest.raises(ConfigError, match="Invalid TOML"):
            load(config_file=cfg_file)

    def test_raises_on_invalid_workers(self, tmp_path):
        cfg_file = tmp_path / "cfg.toml"
        cfg_file.write_text("[upload]\nworkers = 0\n")

        with pytest.raises(ConfigError, match="workers must be at least 1"):
            load(config_file=cfg_file)

    def test_raises_on_invalid_compression_level(self, tmp_path):
        cfg_file = tmp_path / "cfg.toml"
        cfg_file.write_text('[compression]\nlevel = "ultra"\n')

        with pytest.raises(ConfigError, match="Invalid compression level"):
            load(config_file=cfg_file)

    def test_env_var_workers_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GPHOTOS_WORKERS", "12")
        monkeypatch.setattr("uploader.config.USER_CONFIG_PATH", tmp_path / "none.toml")
        monkeypatch.chdir(tmp_path)

        cfg = load()

        assert cfg.workers == 12

    def test_env_var_invalid_integer_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GPHOTOS_WORKERS", "notanumber")
        monkeypatch.setattr("uploader.config.USER_CONFIG_PATH", tmp_path / "none.toml")
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ConfigError, match="must be an integer"):
            load()

    def test_include_extensions_normalised(self, tmp_path):
        cfg_file = tmp_path / "cfg.toml"
        cfg_file.write_text('[scan]\ninclude_extensions = ["jpg", ".PNG"]\n')

        cfg = load(config_file=cfg_file)

        assert cfg.include_extensions == frozenset({".jpg", ".PNG"})
