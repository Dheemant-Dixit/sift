"""
Shared fixtures.

Two things every test needs: settings pointed at a temp folder instead of the
developer's real Downloads, and no dependency on a model server. Neither is
optional — a test suite that reads your actual Downloads folder or needs Ollama
running is a test suite that fails in CI and on everyone else's machine.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np
import pytest

from sift import config
from sift.config import configure

FAKE_DIM = 16


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Point sift at temp directories, ignoring the developer's environment.

    Autouse, because the default source folder is the real Downloads: a test
    that forgets to isolate itself doesn't fail, it quietly reads the
    developer's actual files.

    The temp paths go in via the ENVIRONMENT rather than configure(), because
    configure() replaces its overrides wholesale. A test that calls
    configure(max_file_mb=0) would otherwise drop the isolation and escape to
    the real folder — which is exactly what happened when this was written the
    other way round.
    """
    for key in [k for k in os.environ if k.startswith("SIFT_")]:
        monkeypatch.delenv(key, raising=False)

    source = tmp_path / "downloads"
    source.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("SIFT_SOURCE", str(source))
    monkeypatch.setenv("SIFT_DATA_DIR", str(data))

    settings = configure()
    yield settings
    config.reset()  # cannot raise, even if the test left settings invalid


@pytest.fixture
def source_dir(isolated_settings) -> Path:
    return isolated_settings.source_dir


def write_file(directory: Path, name: str, content: str, age_seconds: float = 60) -> Path:
    """Write a file and backdate it past the still-being-written guard.

    ingest skips files modified in the last couple of seconds, on the assumption
    they're half-downloaded. A file a test just created looks exactly like that,
    so tests age it deliberately rather than patching the guard away — the guard
    itself stays under test.
    """
    path = directory / name
    path.write_text(content, encoding="utf-8")
    past = path.stat().st_mtime - age_seconds
    os.utime(path, (past, past))
    return path


@pytest.fixture
def make_file(source_dir):
    def _make(name: str, content: str, age_seconds: float = 60) -> Path:
        return write_file(source_dir, name, content, age_seconds)
    return _make


def fake_embed(texts: list[str]) -> np.ndarray:
    """A deterministic stand-in for a real embedding model.

    Hashes each text into a fixed-width vector. Nonsense semantically, but
    stable and identical-text-gives-identical-vector, which is all the index
    machinery actually cares about.
    """
    vectors = []
    for text in texts:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw = np.frombuffer(digest[:FAKE_DIM], dtype=np.uint8).astype(np.float32)
        vectors.append(raw / 255.0)
    return np.array(vectors, dtype=np.float32)


@pytest.fixture
def embedder():
    return fake_embed
