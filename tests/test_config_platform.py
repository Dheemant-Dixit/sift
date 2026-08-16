"""
Tests for where sift decides its folders live, on each platform.

test_config.py already covers precedence (flag > env > .env > default). This
file covers the other half: the per-platform path resolution that the CI matrix
claims to support on macOS, Linux and Windows but that no test exercised.

The two that matter and are easy to get wrong:

  · Linux Downloads is NOT ~/Downloads on a localized desktop. On a French
    install it is `Téléchargements`, named in user-dirs.dirs. Hardcoding the
    English name finds an empty folder and reports "nothing to index".

  · The data dir must never be the install directory. A pip-installed package
    writing next to its own source could be writing into site-packages.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sift_downloads import config
from sift_downloads.config import (ConfigError, _xdg_downloads, configure,
                                   default_data_dir, default_downloads_dir,
                                   get_settings, require_source_dir)


@pytest.fixture
def as_platform(monkeypatch):
    def _set(system):
        monkeypatch.setattr(config.platform, "system", lambda: system)
    return _set


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


# --- Linux: the localized-folder problem ------------------------------------

def write_user_dirs(home: Path, monkeypatch, contents: str) -> None:
    config_home = home / ".config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "user-dirs.dirs").write_text(contents, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))


def test_a_localized_downloads_folder_is_found(home, monkeypatch, as_platform):
    as_platform("Linux")
    write_user_dirs(home, monkeypatch, 'XDG_DOWNLOAD_DIR="$HOME/Téléchargements"\n')
    assert default_downloads_dir() == home / "Téléchargements"


def test_home_is_expanded_from_the_literal_string(home, monkeypatch):
    """user-dirs.dirs stores "$HOME/..." unexpanded."""
    write_user_dirs(home, monkeypatch, 'XDG_DOWNLOAD_DIR="$HOME/Downloads"\n')
    assert _xdg_downloads() == home / "Downloads"


def test_other_xdg_entries_are_ignored(home, monkeypatch):
    write_user_dirs(home, monkeypatch,
                    'XDG_DESKTOP_DIR="$HOME/Bureau"\n'
                    'XDG_DOWNLOAD_DIR="$HOME/Téléchargements"\n'
                    'XDG_MUSIC_DIR="$HOME/Musique"\n')
    assert _xdg_downloads() == home / "Téléchargements"


def test_an_absolute_path_is_used_as_is(home, monkeypatch):
    write_user_dirs(home, monkeypatch, 'XDG_DOWNLOAD_DIR="/mnt/big-disk/dl"\n')
    assert _xdg_downloads() == Path("/mnt/big-disk/dl")


def test_a_missing_user_dirs_file_is_not_an_error(home, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "nonexistent"))
    assert _xdg_downloads() is None


def test_a_user_dirs_file_without_a_download_entry_returns_nothing(home, monkeypatch):
    write_user_dirs(home, monkeypatch, 'XDG_DESKTOP_DIR="$HOME/Desktop"\n')
    assert _xdg_downloads() is None


def test_an_unparseable_line_does_not_crash(home, monkeypatch):
    write_user_dirs(home, monkeypatch, 'XDG_DOWNLOAD_DIR="unclosed\n')
    assert _xdg_downloads() is None


def test_linux_falls_back_to_english_downloads(home, monkeypatch, as_platform):
    as_platform("Linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "nonexistent"))
    assert default_downloads_dir() == home / "Downloads"


# --- macOS and Windows ------------------------------------------------------

def test_macos_uses_home_downloads(home, as_platform, monkeypatch):
    as_platform("Darwin")
    monkeypatch.setattr(config, "_xdg_downloads",
                        lambda: pytest.fail("macOS must not read user-dirs.dirs"))
    assert default_downloads_dir() == home / "Downloads"


def test_windows_asks_the_system_first(home, as_platform, monkeypatch):
    """Downloads is relocatable and OneDrive redirects it, so %USERPROFILE% is a guess."""
    as_platform("Windows")
    monkeypatch.setattr(config, "_windows_downloads", lambda: Path("D:/Downloads"))
    assert default_downloads_dir() == Path("D:/Downloads")


def test_windows_falls_back_to_userprofile(home, as_platform, monkeypatch):
    as_platform("Windows")
    monkeypatch.setattr(config, "_windows_downloads", lambda: None)
    monkeypatch.setenv("USERPROFILE", "C:/Users/dheemant")
    assert default_downloads_dir() == Path("C:/Users/dheemant") / "Downloads"


@pytest.mark.skipif(sys.platform == "win32",
                    reason="on Windows the probe legitimately returns a path")
def test_the_windows_probe_returns_none_off_windows():
    """It imports windll, which does not exist here; it must not raise."""
    assert config._windows_downloads() is None


# --- the data dir must never be the install dir -----------------------------

@pytest.mark.parametrize("system, expected_parts", [
    ("Darwin", ("Library", "Application Support", "sift")),
    ("Linux", (".local", "share", "sift")),
])
def test_the_data_dir_is_a_per_user_location(home, as_platform, monkeypatch,
                                             system, expected_parts):
    as_platform(system)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    parts = default_data_dir().parts
    for expected in expected_parts:
        assert expected in parts


def test_xdg_data_home_is_respected(home, as_platform, monkeypatch):
    as_platform("Linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "custom-data"))
    assert default_data_dir() == home / "custom-data" / "sift"


def test_windows_uses_localappdata(home, as_platform, monkeypatch):
    as_platform("Windows")
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/d/AppData/Local")
    assert default_data_dir() == Path("C:/Users/d/AppData/Local") / "sift"


def test_the_data_dir_is_never_inside_the_package(home, as_platform):
    as_platform("Darwin")
    package_dir = Path(config.__file__).resolve().parent
    assert package_dir not in default_data_dir().resolve().parents


def test_the_app_name_is_still_sift(home, as_platform):
    """Changing it would orphan every existing index on upgrade."""
    as_platform("Darwin")
    assert default_data_dir().name == "sift"


# --- require_source_dir: an instruction, not a FileNotFoundError ------------

def test_an_existing_folder_is_returned(source_dir):
    assert require_source_dir(get_settings()) == source_dir


def test_a_missing_folder_names_both_ways_to_fix_it(tmp_path):
    configure(source_dir=tmp_path / "gone")
    with pytest.raises(ConfigError) as e:
        require_source_dir(get_settings())
    assert "--source" in str(e.value) and "SIFT_SOURCE" in str(e.value)


def test_a_file_where_a_folder_should_be_says_so(tmp_path):
    target = tmp_path / "not-a-folder"
    target.write_text("i am a file")
    configure(source_dir=target)
    with pytest.raises(ConfigError, match="not a folder"):
        require_source_dir(get_settings())


# --- validation catches configurations that would fail later ----------------

def test_an_overlap_as_large_as_the_chunk_is_rejected():
    """Without this, chunking cannot advance and would loop forever."""
    with pytest.raises(ConfigError, match="must be smaller"):
        configure(chunk_size=200, chunk_overlap=200)


def test_an_overlap_larger_than_the_chunk_is_rejected():
    with pytest.raises(ConfigError, match="must be smaller"):
        configure(chunk_size=200, chunk_overlap=500)


def test_a_top_k_below_one_is_rejected():
    with pytest.raises(ConfigError, match="at least 1"):
        configure(top_k=0)


def test_paths_are_expanded(tmp_path, monkeypatch):
    """A literal '~' reaching Path.iterdir() would look for a folder named '~'."""
    monkeypatch.setenv("HOME", str(tmp_path))       # what expanduser() reads
    settings = configure(source_dir=Path("~/somewhere"), data_dir=Path("~/data"))
    assert "~" not in str(settings.source_dir)
    assert settings.source_dir.is_absolute()
    assert settings.data_dir.is_absolute()


# --- environment variables are parsed, not trusted --------------------------

@pytest.mark.parametrize("var, value", [
    ("SIFT_TOP_K", "not-a-number"),
    ("SIFT_CHUNK_SIZE", "1.5.2"),
])
def test_a_non_integer_env_var_is_a_clear_error(monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    with pytest.raises(ConfigError, match="must be an integer"):
        configure()


def test_a_non_numeric_float_env_var_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("SIFT_MIN_SCORE", "very relevant")
    with pytest.raises(ConfigError, match="must be a number"):
        configure()


@pytest.mark.parametrize("raw, expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("", False), ("maybe", False),
])
def test_boolean_env_vars_accept_the_obvious_spellings(monkeypatch, raw, expected):
    monkeypatch.setenv("SIFT_ALLOW_CLOUD", raw)
    assert configure().allow_cloud is expected
