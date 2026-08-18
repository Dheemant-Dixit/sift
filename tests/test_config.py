"""
Tests for settings resolution.

The whole reason config.py looks the way it does is that the obvious
alternative — module-level constants bound into default arguments — silently
ignores every override. These tests are what prove the flags actually work.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sift_downloads import config
from sift_downloads.config import ConfigError, Settings, configure, get_settings, require_source_dir


def test_defaults_apply_with_nothing_set(monkeypatch):
    monkeypatch.delenv("SIFT_TOP_K", raising=False)
    configure()
    assert get_settings().top_k == Settings().top_k


def test_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("SIFT_TOP_K", "11")
    configure()
    assert get_settings().top_k == 11


def test_explicit_override_beats_env(monkeypatch):
    """A CLI flag has to win over the environment, or --top-k is decoration."""
    monkeypatch.setenv("SIFT_TOP_K", "11")
    configure(top_k=3)
    assert get_settings().top_k == 3


def test_unset_override_does_not_clobber_env(monkeypatch):
    """configure(top_k=None) means 'the user didn't pass --top-k', not 'use None'."""
    monkeypatch.setenv("SIFT_TOP_K", "11")
    configure(top_k=None, min_score=0.7)
    settings = get_settings()
    assert settings.top_k == 11
    assert settings.min_score == 0.7


def test_configure_busts_the_settings_cache(tmp_path):
    first = configure(source_dir=tmp_path / "one").source_dir
    second = configure(source_dir=tmp_path / "two").source_dir
    assert first != second
    assert get_settings().source_dir == tmp_path / "two"


def test_configure_busts_the_store_cache(tmp_path, monkeypatch):
    """A stale store cache would answer the new --source with the old index."""
    from sift_downloads import retrieve

    retrieve.get_store.cache_clear()
    retrieve.get_store()  # populate
    assert retrieve.get_store.cache_info().currsize == 1

    configure(source_dir=tmp_path / "elsewhere")
    assert retrieve.get_store.cache_info().currsize == 0


def test_bad_env_value_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("SIFT_TOP_K", "lots")
    with pytest.raises(ConfigError, match="must be an integer"):
        configure()


def test_overlap_larger_than_chunk_is_rejected():
    """Chunking could not advance; better to say so than to loop oddly."""
    with pytest.raises(ConfigError, match="smaller than"):
        configure(chunk_size=100, chunk_overlap=100)


def test_missing_source_dir_explains_the_fix(tmp_path):
    configure(source_dir=tmp_path / "not-there")
    with pytest.raises(ConfigError, match="--source"):
        require_source_dir()


def test_index_lives_outside_the_repo(tmp_path):
    """The index must never be written next to the installed package."""
    settings = configure(data_dir=tmp_path / "data")
    assert settings.index_path.parent == tmp_path / "data"
    assert settings.manifest_path.parent == tmp_path / "data"


# --- locating Downloads ----------------------------------------------------

def test_downloads_falls_back_to_home(monkeypatch):
    monkeypatch.setattr(config.platform, "system", lambda: "Darwin")
    assert config.default_downloads_dir() == Path.home() / "Downloads"


def test_linux_reads_localized_folder_from_user_dirs(monkeypatch, tmp_path):
    """On a French desktop the folder is 'Téléchargements' — ~/Downloads finds nothing."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "user-dirs.dirs").write_text(
        '# generated\nXDG_DESKTOP_DIR="$HOME/Bureau"\n'
        'XDG_DOWNLOAD_DIR="$HOME/Téléchargements"\n',
        encoding="utf-8")

    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    assert config.default_downloads_dir() == Path.home() / "Téléchargements"


def test_linux_without_user_dirs_falls_back(monkeypatch, tmp_path):
    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    assert config.default_downloads_dir() == Path.home() / "Downloads"


def test_windows_falls_back_to_userprofile(monkeypatch, tmp_path):
    monkeypatch.setattr(config.platform, "system", lambda: "Windows")
    monkeypatch.setattr(config, "_windows_downloads", lambda: None)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert config.default_downloads_dir() == tmp_path / "Downloads"


def test_data_dir_is_platform_appropriate(monkeypatch, tmp_path):
    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert config.default_data_dir() == tmp_path / "sift"


# --- the cloud gate --------------------------------------------------------

def test_local_models_need_no_consent():
    settings = configure()
    assert not settings.uses_cloud()
    config.check_cloud_consent(settings)  # must not raise


def test_cloud_model_is_refused_without_consent():
    settings = configure(chat_model="anthropic/claude-sonnet-4-5")
    assert settings.uses_cloud()
    with pytest.raises(ConfigError, match="--allow-cloud"):
        config.check_cloud_consent(settings)


def test_cloud_model_allowed_with_explicit_consent():
    settings = configure(chat_model="anthropic/claude-sonnet-4-5", allow_cloud=True)
    config.check_cloud_consent(settings)  # must not raise


def test_negative_overlap_is_rejected():
    """Bounded from above already; unbounded below, `chunk_text` advances past
    each window and the gap between them is never indexed — no error, no log."""
    with pytest.raises(ConfigError, match="negative"):
        configure(chunk_overlap=-100)
