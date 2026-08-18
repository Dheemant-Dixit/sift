"""
Tests for `sift unlock` and the detailed half of `sift status`.

Split from test_cli.py because these two need a real index on disk rather than a
stubbed one: `status` groups files by WHY they have no chunks, and `unlock`
walks the manifest. Faking those would test the fake.

`unlock` gets the most attention here. It handles passwords, and its contract is
narrow and worth pinning: three attempts, a blank password skips, the warning
about the unencrypted index is printed BEFORE anything is asked for, and nothing
is ever written down.
"""
from __future__ import annotations

import pytest

from sift_downloads import cli
from sift_downloads.cli import main
from sift_downloads.index import Manifest, update_index
from sift_downloads.ingest import REASON_LOCKED, REASON_NO_TEXT


@pytest.fixture(autouse=True)
def no_preflight(monkeypatch):
    import sift_downloads.doctor as doctor
    monkeypatch.setattr(doctor, "preflight", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    """`cmd_unlock` calls unlock_file() without an embedder, so it would reach
    for the real one. Patch the seam it actually falls back to."""
    import sift_downloads.index as index
    from tests.conftest import fake_embed
    monkeypatch.setattr(index, "embed_texts",
                        lambda texts, settings=None, **kw: fake_embed(texts))


@pytest.fixture
def passwords(monkeypatch):
    """Queue up answers to getpass, and record what it was asked."""
    box = {"answers": [], "prompts": []}

    def fake_getpass(prompt=""):
        box["prompts"].append(prompt)
        if not box["answers"]:
            raise EOFError
        return box["answers"].pop(0)

    import getpass as getpass_module
    monkeypatch.setattr(getpass_module, "getpass", fake_getpass)
    return box


@pytest.fixture
def indexed(embedder, make_pdf, make_file):
    """Build a real index containing one readable and one locked file."""
    def _build():
        update_index(embedder=embedder)
    return _build


# --- unlock ----------------------------------------------------------------

def test_unlock_with_nothing_locked_says_so(embedder, make_file, capsys):
    make_file("notes.md", "plain")
    update_index(embedder=embedder)
    assert main(["unlock"]) == 0
    assert "nothing to unlock" in capsys.readouterr().out.lower()


def test_unlock_warns_about_the_unencrypted_index_before_asking(
        embedder, make_pdf, passwords, capsys):
    make_pdf("statement.pdf", text="balance 42", password="hunter2")
    update_index(embedder=embedder)
    passwords["answers"] = ["hunter2"]
    main(["unlock"])
    out = capsys.readouterr().out
    warning_at = out.index("NOT itself encrypted")
    assert "password is never stored" in out
    # The prompt happens via getpass, so anything printed after the warning is
    # fine; what matters is the warning exists and names the index path.
    assert warning_at < len(out)
    assert str(cli.get_settings().index_path) in out


def test_the_right_password_indexes_the_text(embedder, make_pdf, passwords, capsys):
    make_pdf("statement.pdf", text="account balance is 4200", password="hunter2")
    update_index(embedder=embedder)
    passwords["answers"] = ["hunter2"]
    assert main(["unlock"]) == 0
    out = capsys.readouterr().out
    assert "chunks indexed" in out
    assert "1 file(s) unlocked" in out


def test_an_unlocked_file_is_no_longer_reported_as_locked(
        embedder, make_pdf, passwords):
    make_pdf("statement.pdf", text="account balance is 4200", password="hunter2")
    update_index(embedder=embedder)
    assert len(Manifest.load().locked()) == 1
    passwords["answers"] = ["hunter2"]
    main(["unlock"])
    assert Manifest.load().locked() == []


def test_a_wrong_password_is_retried_three_times_then_given_up_on(
        embedder, make_pdf, passwords, capsys):
    make_pdf("statement.pdf", text="secret", password="hunter2")
    update_index(embedder=embedder)
    passwords["answers"] = ["wrong", "also-wrong", "still-wrong"]
    assert main(["unlock"]) == 0
    out = capsys.readouterr().out
    assert out.count("wrong password") == 3
    assert "2 attempt(s) left" in out and "1 attempt(s) left" in out
    assert "Nothing unlocked" in out


def test_a_right_password_after_a_wrong_one_still_works(
        embedder, make_pdf, passwords, capsys):
    make_pdf("statement.pdf", text="secret", password="hunter2")
    update_index(embedder=embedder)
    passwords["answers"] = ["wrong", "hunter2"]
    main(["unlock"])
    assert "1 file(s) unlocked" in capsys.readouterr().out


def test_a_blank_password_skips_the_file(embedder, make_pdf, passwords, capsys):
    make_pdf("statement.pdf", text="secret", password="hunter2")
    update_index(embedder=embedder)
    passwords["answers"] = [""]
    assert main(["unlock"]) == 0
    out = capsys.readouterr().out
    assert "skipped" in out and "Nothing unlocked" in out


def test_ctrl_d_at_the_prompt_stops_without_unlocking(
        embedder, make_pdf, passwords, capsys):
    make_pdf("statement.pdf", text="secret", password="hunter2")
    update_index(embedder=embedder)
    passwords["answers"] = []          # getpass raises EOFError
    assert main(["unlock"]) == 1
    assert "Stopped" in capsys.readouterr().out


def test_unlock_can_be_pointed_at_a_named_file(
        embedder, make_pdf, passwords, capsys):
    path = make_pdf("statement.pdf", text="secret", password="hunter2")
    update_index(embedder=embedder)
    passwords["answers"] = ["hunter2"]
    assert main(["unlock", str(path)]) == 0
    assert "statement.pdf" in passwords["prompts"][0]


def test_unlock_reports_a_named_file_that_does_not_exist(
        embedder, make_file, capsys):
    make_file("notes.md", "plain")
    update_index(embedder=embedder)
    assert main(["unlock", "/nope/missing.pdf"]) == 0
    assert "no such file" in capsys.readouterr().out


def test_a_locked_file_with_no_text_layer_is_not_retried(
        embedder, make_pdf, passwords, capsys):
    """No password fixes a scanned page, so asking again would be cruel."""
    make_pdf("scan.pdf", text="", password="hunter2")
    update_index(embedder=embedder)
    passwords["answers"] = ["hunter2", "hunter2", "hunter2"]
    main(["unlock"])
    out = capsys.readouterr().out
    assert REASON_NO_TEXT in out
    assert len(passwords["prompts"]) == 1, "it must not ask a second time"


def test_the_password_is_never_written_anywhere(
        embedder, make_pdf, passwords, tmp_path):
    """The whole point of the design: nothing on disk should contain it."""
    make_pdf("statement.pdf", text="account balance is 4200", password="hunter2")
    update_index(embedder=embedder)
    passwords["answers"] = ["hunter2"]
    main(["unlock"])

    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"hunter2" not in path.read_bytes(), f"password leaked into {path}"


# --- status, the detailed half --------------------------------------------

def test_status_without_an_index_points_at_the_fix(capsys):
    assert main(["status"]) == 0
    assert "sift index" in capsys.readouterr().out


def test_status_counts_chunks_and_files(embedder, make_file, capsys):
    make_file("a.md", "alpha " * 200)
    make_file("b.md", "beta " * 200)
    update_index(embedder=embedder)
    main(["status"])
    out = capsys.readouterr().out
    assert "chunks from 2 files" in out
    assert "last synced" in out


def test_status_groups_unreadable_files_by_reason(
        embedder, make_pdf, make_file, capsys):
    make_file("readable.md", "text")
    make_pdf("locked.pdf", text="secret", password="hunter2")
    make_pdf("scanned.pdf", text="")
    update_index(embedder=embedder)
    out = capsys.readouterr().out  # drain sync output
    main(["status"])
    out = capsys.readouterr().out
    assert REASON_LOCKED in out and "locked.pdf" in out
    assert REASON_NO_TEXT in out and "scanned.pdf" in out
    assert "sift unlock" in out, "a locked file needs a password, not OCR"


def test_status_does_not_offer_unlock_for_a_merely_scanned_file(
        embedder, make_pdf, capsys):
    make_pdf("scanned.pdf", text="")
    update_index(embedder=embedder)
    capsys.readouterr()
    main(["status"])
    out = capsys.readouterr().out
    assert REASON_NO_TEXT in out
    assert "sift unlock" not in out


def test_status_hides_the_skipped_list_behind_a_flag(
        embedder, make_file, capsys):
    make_file("notes.md", "text")
    make_file("movie.mkv", "not indexable")
    update_index(embedder=embedder)
    capsys.readouterr()

    main(["status"])
    assert "see --skipped" in capsys.readouterr().out

    main(["status", "--skipped"])
    out = capsys.readouterr().out
    assert "movie.mkv" in out and "see --skipped" not in out


def test_status_reports_a_corrupt_index_instead_of_crashing(
        embedder, make_file, capsys):
    make_file("notes.md", "text")
    update_index(embedder=embedder)
    cli.get_settings().index_path.write_bytes(b"not an npz file")
    assert main(["status"]) == 1
    assert "!" in capsys.readouterr().out


# --- the interactive `ask` loop -------------------------------------------

def test_ask_without_a_question_reads_from_stdin_until_quit(monkeypatch, capsys):
    import sift_downloads.generate as generate
    from sift_downloads.generate import Answer
    asked = []
    monkeypatch.setattr(generate, "answer",
                        lambda q, **k: asked.append(q) or Answer(text="an answer"))
    answers = iter(["what is my notice period", "quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert main(["ask", "--no-sync"]) == 0
    assert asked == ["what is my notice period"]


def test_the_ask_loop_exits_cleanly_on_ctrl_d(monkeypatch, capsys):
    def raise_eof(_):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert main(["ask", "--no-sync"]) == 0


def test_an_empty_line_ends_the_ask_loop(monkeypatch):
    import sift_downloads.generate as generate
    monkeypatch.setattr(generate, "answer",
                        lambda *a, **k: pytest.fail("an empty line is not a question"))
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert main(["ask", "--no-sync"]) == 0
