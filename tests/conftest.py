"""
Shared fixtures.

Two things every test needs: settings pointed at a temp folder instead of the
developer's real Downloads, and no dependency on a model server. Neither is
optional — a test suite that reads your actual Downloads folder or needs Ollama
running is a test suite that fails in CI and on everyone else's machine.
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import numpy as np
import pytest

from sift_downloads import config
from sift_downloads.config import configure

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


@pytest.fixture(autouse=True)
def no_model_server(monkeypatch):
    """Make reaching a model server impossible, rather than merely unusual.

    Autouse and unconditional. Passing a fake embedder to the function under
    test is easy to do in most places and easy to forget in one — and the
    forgotten test still passes on a laptop with Ollama running, then fails in
    CI. Six tests were written that way before this existed.

    A test that legitimately exercises an embedding path replaces the seam it
    needs (`index.embed_texts`, or an `embedder=` argument); anything that
    reaches litellm itself is a mistake and says so here.
    """
    import litellm

    def refuse(*args, **kwargs):
        raise AssertionError(
            "this test reached litellm, which means it needs a model server.\n"
            "Pass embedder=fake_embed, or monkeypatch index.embed_texts."
        )

    monkeypatch.setattr(litellm, "embedding", refuse)
    monkeypatch.setattr(litellm, "completion", refuse)


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


# --- synthetic PDFs --------------------------------------------------------
#
# Built from bytes rather than committed as fixture files. A checked-in binary
# can't show you why it's locked or text-free, can't be varied, and in this repo
# would mean committing a real document. These are ~20 readable lines instead.

def minimal_pdf(text: str = "") -> bytes:
    """A valid one-page PDF. An empty `text` gives a page with no text layer,
    which is what a scanned document looks like to pypdf."""
    draw = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode() if text else b""
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 300]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>stream\n%s\nendstream" % (len(draw), draw),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj" % number + body + b"endobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, xref_at))
    return bytes(out)


def write_pdf(directory: Path, name: str, text: str = "",
              password: str | None = None, age_seconds: float = 60) -> Path:
    """Write a PDF, optionally password-protected. Backdated like write_file."""
    raw = minimal_pdf(text)
    if password is not None:
        from pypdf import PdfReader, PdfWriter
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(raw)))
        writer.encrypt(password)
        buffer = io.BytesIO()
        writer.write(buffer)
        raw = buffer.getvalue()

    path = directory / name
    path.write_bytes(raw)
    past = path.stat().st_mtime - age_seconds
    os.utime(path, (past, past))
    return path


@pytest.fixture
def make_pdf(source_dir):
    def _make(name: str, text: str = "", password: str | None = None,
              age_seconds: float = 60) -> Path:
        return write_pdf(source_dir, name, text, password, age_seconds)
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
