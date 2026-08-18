"""
Tests for file-level ranking.

The content half is tested by stubbing retrieval with canned chunk hits — the
question here is how chunk scores become file scores, not whether the embedding
model is any good.
"""
from __future__ import annotations

import pytest

from sift_downloads import find as find_module
from sift_downloads.find import filename_score, find_files, tokenize
from sift_downloads.index import Manifest
from sift_downloads.ingest import REASON_LOCKED, REASON_NO_TEXT


@pytest.fixture
def stub_search(monkeypatch):
    """Replace retrieval with fixed chunk hits."""
    def _install(hits):
        monkeypatch.setattr(find_module, "search",
                            lambda query, top_k=None, min_score=None, settings=None: hits)
    return _install


def _chunk(path, score, text="some passage", index=0):
    from pathlib import Path
    return {"path": str(path), "filename": Path(path).name, "chunk_index": index,
            "text": text, "score": score, "rank": 1, "id": 0}


def _manifest_with(paths, skipped=None, duplicates=None, chunks=3, reason=""):
    manifest = Manifest(
        files={str(p): {"size": 10, "mtime": 0, "num_chunks": chunks,
                        "reason": reason} for p in paths},
        skipped=skipped or {},
        duplicates=duplicates or {},
    )
    manifest.save()
    return manifest


# --- tokenizing ------------------------------------------------------------

def test_tokenize_splits_camel_case_and_separators():
    assert tokenize("RentalAgreement_2024-final.pdf") == \
        ["rental", "agreement", "2024", "final", "pdf"]


def test_filename_score_rewards_a_complete_match():
    assert filename_score({"rental", "agreement"}, "RentalAgreement.pdf") == pytest.approx(0.98)


def test_filename_score_ignores_a_weak_match():
    """One word out of four isn't evidence; it would surface noise."""
    assert filename_score({"rental", "agreement", "signed", "final"}, "agreement.pdf") == 0.0


def test_filename_score_is_moderate_for_a_partial_match():
    score = filename_score({"bank", "statement"}, "statement.pdf")
    assert 0.4 < score < 0.98


# --- ranking ---------------------------------------------------------------

def test_file_scored_by_its_best_chunk_not_its_average(make_file, stub_search):
    """A forty-page statement with one matching page is still the right file."""
    long_doc = make_file("long.md", "x")
    short_doc = make_file("short.md", "x")
    _manifest_with([long_doc, short_doc])

    stub_search([
        _chunk(long_doc, 0.82, index=0),
        _chunk(long_doc, 0.31, index=1),
        _chunk(long_doc, 0.29, index=2),
        _chunk(long_doc, 0.28, index=3),
        _chunk(short_doc, 0.70),
    ])

    hits = find_files("anything")
    assert hits[0].name == "long.md"          # mean would have ranked it last
    assert hits[0].score >= 0.82


def test_several_matching_chunks_break_a_tie(make_file, stub_search):
    one = make_file("one.md", "x")
    many = make_file("many.md", "x")
    _manifest_with([one, many])

    stub_search([
        _chunk(one, 0.70),
        _chunk(many, 0.70, index=0),
        _chunk(many, 0.69, index=1),
        _chunk(many, 0.68, index=2),
    ])

    hits = find_files("anything")
    assert hits[0].name == "many.md"
    assert hits[0].score - hits[1].score < 0.1   # a nudge, not a reordering force


def test_file_with_no_extractable_text_is_still_found_by_name(make_file, stub_search):
    """The case that matters: a scanned PDF has no chunks and no content score.

    Without filename matching, typing "rental agreement" would return nothing at
    all for the exact file the user is looking for.
    """
    scanned = make_file("RentalAgreement.pdf", "x")
    _manifest_with([scanned], chunks=0, reason=REASON_NO_TEXT)
    stub_search([])   # no chunks exist for it

    hits = find_files("rental agreement")
    assert [h.name for h in hits] == ["RentalAgreement.pdf"]
    assert hits[0].matched_on == "filename"
    assert hits[0].indexed is False
    assert "no text layer" in hits[0].note


def test_a_locked_file_is_told_to_unlock_not_to_ocr(make_file, stub_search):
    """The misdiagnosis, at the surface the user actually reads."""
    statement = make_file("AccountStatement.pdf", "x")
    _manifest_with([statement], chunks=0, reason=REASON_LOCKED)
    stub_search([])

    note = find_files("account statement")[0].note
    assert "password-protected" in note
    assert "sift unlock" in note
    assert "scanned" not in note


def test_an_old_manifest_without_a_reason_still_explains_itself(make_file, stub_search):
    """Written before reasons were recorded: fall back, never show a blank."""
    scanned = make_file("Scan.pdf", "x")
    _manifest_with([scanned], chunks=0, reason="")
    stub_search([])

    assert find_files("scan")[0].note == REASON_NO_TEXT


def test_unindexable_file_types_are_still_findable(make_file, stub_search):
    """A .zip can't be read, but "where did that zip go" is a real question."""
    archive = make_file("ProjectBackup.zip", "x")
    _manifest_with([], skipped={str(archive): "unsupported type"})
    stub_search([])

    hits = find_files("project backup")
    assert [h.name for h in hits] == ["ProjectBackup.zip"]
    assert hits[0].note == "unsupported type"


def test_noise_files_are_not_listed(make_file, stub_search):
    partial = make_file("Invoice.pdf.crdownload", "x")
    _manifest_with([], skipped={str(partial): "download in progress"})
    stub_search([])
    assert find_files("invoice") == []


def test_matched_on_reports_both_signals(make_file, stub_search):
    doc = make_file("BankStatement.md", "x")
    _manifest_with([doc])
    stub_search([_chunk(doc, 0.72)])

    hits = find_files("bank statement")
    assert hits[0].matched_on == "both"


def test_stopwords_do_not_dilute_a_filename_match(make_file, stub_search):
    """"what is my rental agreement" must match as well as "rental agreement"."""
    doc = make_file("RentalAgreement.pdf", "x")
    _manifest_with([doc], chunks=0)
    stub_search([])

    hits = find_files("what is my rental agreement")
    assert hits and hits[0].matched_on == "filename"
    assert hits[0].score == pytest.approx(0.98)


def test_duplicates_are_reported_on_the_canonical_hit(make_file, stub_search):
    canonical = make_file("Statement.md", "x")
    copy = make_file("Statement (1).md", "x")
    _manifest_with([canonical], duplicates={str(copy): str(canonical)})
    stub_search([_chunk(canonical, 0.80)])

    hits = find_files("statement")
    assert len(hits) == 1                       # the copy isn't a separate result
    assert hits[0].duplicates == [str(copy)]


def test_recent_flag_favours_newer_files(make_file, stub_search):
    old = make_file("old.md", "x", age_seconds=200 * 86400)
    new = make_file("new.md", "x", age_seconds=60)
    _manifest_with([old, new])
    stub_search([_chunk(old, 0.75), _chunk(new, 0.70)])

    assert find_files("anything")[0].name == "old.md"           # relevance alone
    assert find_files("anything", recent_first=True)[0].name == "new.md"


def test_limit_is_respected(make_file, stub_search):
    docs = [make_file(f"doc{i}.md", "x") for i in range(8)]
    _manifest_with(docs)
    stub_search([_chunk(d, 0.9 - i * 0.01) for i, d in enumerate(docs)])
    assert len(find_files("anything", limit=3)) == 3


def test_deleted_file_is_not_returned(make_file, stub_search):
    doc = make_file("gone.md", "x")
    _manifest_with([doc])
    stub_search([_chunk(doc, 0.9)])
    doc.unlink()
    assert find_files("anything") == []


def test_find_degrades_to_filenames_when_retrieval_fails(make_file, monkeypatch):
    """Ollama being down should narrow the results, not fail the command."""
    doc = make_file("TaxReturn2024.pdf", "x")
    _manifest_with([doc])

    def explode(*args, **kwargs):
        raise ConnectionError("ollama is not running")

    monkeypatch.setattr(find_module, "search", explode)

    hits = find_files("tax return")
    assert [h.name for h in hits] == ["TaxReturn2024.pdf"]
    assert hits[0].matched_on == "filename"
