"""
Tests for the preflight checks.

doctor.py exists so a stranger with no Ollama gets an instruction instead of a
litellm traceback. So the assertions here are mostly about the *fix* string:
a check that fails without saying what to do has not done its job.

Ollama is never contacted. `_installed_ollama_models` is the single seam where
this module reaches the network, and it is stubbed throughout.
"""
from __future__ import annotations

import pytest

from sift_downloads import doctor
from sift_downloads.config import ConfigError, configure, get_settings
from sift_downloads.doctor import (
    FAIL,
    OK,
    WARN,
    Check,
    check_index,
    check_models,
    check_privacy,
    check_source,
    preflight,
    run_checks,
)


def both() -> tuple[str, str]:
    """Both configured models — what a command that embeds and answers passes."""
    s = get_settings()
    return (s.embed_model, s.chat_model)


@pytest.fixture
def ollama(monkeypatch):
    """Control what Ollama appears to have installed. None = not running."""
    box = {"models": ["nomic-embed-text:latest", "llama3.1:8b"]}
    monkeypatch.setattr(doctor, "_installed_ollama_models", lambda: box["models"])
    return box


# --- Check ------------------------------------------------------------------

def test_only_a_fail_counts_as_failed():
    assert Check("x", FAIL, "d").failed is True
    assert Check("x", WARN, "d").failed is False
    assert Check("x", OK, "d").failed is False


# --- models -----------------------------------------------------------------

def test_ollama_down_reports_one_failure_with_an_install_command(ollama):
    ollama["models"] = None
    checks = check_models(get_settings())
    assert len(checks) == 1
    assert checks[0].failed
    assert "ollama" in checks[0].fix.lower()


def test_both_models_present_passes(ollama):
    checks = check_models(get_settings())
    assert not any(c.failed for c in checks)
    assert any("running at" in c.detail for c in checks)


def test_a_missing_model_points_at_sift_setup_and_says_which_model(ollama):
    """`sift setup` pulls every missing model, so it beats one pull command.

    The model is still named — by the check, which is where a reader looks to
    find out what is wrong rather than what to type.
    """
    ollama["models"] = ["nomic-embed-text:latest"]
    checks = check_models(get_settings())
    failed = [c for c in checks if c.failed]
    assert len(failed) == 1
    assert failed[0].fix == "sift setup"
    assert failed[0].name == "model ollama_chat/llama3.1:8b"


def test_a_model_pulled_without_a_tag_still_counts_as_present(ollama):
    """Ollama reports 'nomic-embed-text:latest' for a plain `ollama pull`."""
    ollama["models"] = ["nomic-embed-text:latest", "llama3.1:8b"]
    configure(embed_model="ollama/nomic-embed-text")
    assert not any(c.failed for c in check_models(get_settings()))


def test_cloud_models_skip_the_ollama_check_entirely(monkeypatch):
    monkeypatch.setattr(doctor, "_installed_ollama_models",
                        lambda: pytest.fail("must not contact Ollama for cloud models"))
    configure(embed_model="openai/text-embedding-3-small",
              chat_model="anthropic/claude-sonnet-4-5")
    checks = check_models(get_settings())
    assert len(checks) == 2 and not any(c.failed for c in checks)


def test_a_mixed_setup_still_checks_the_local_half(ollama):
    ollama["models"] = []
    configure(chat_model="anthropic/claude-sonnet-4-5")   # embed stays local
    failed = [c for c in check_models(get_settings()) if c.failed]
    assert [c.fix for c in failed] == ["sift setup"]


def test_a_local_non_ollama_model_is_never_called_non_local(monkeypatch, make_file):
    """doctor contradicted itself in two adjacent [ok] lines.

    `check_models` knew two local prefixes and `check_privacy` knew five, so an
    LM Studio setup printed "using non-local models: lm_studio/..." directly
    above "fully local - no document text leaves this machine". The report a
    user runs *to be told the truth about privacy* answered the question twice,
    both ways.
    """
    monkeypatch.setattr(doctor, "_installed_ollama_models",
                        lambda: pytest.fail("must not contact Ollama for an LM Studio model"))
    make_file("a.md", "x")
    configure(embed_model="lm_studio/bge-small", chat_model="lm_studio/llama-3.1-8b")

    checks = run_checks(get_settings(), include_index=False)

    privacy = next(c for c in checks if c.name == "privacy")
    assert "fully local" in privacy.detail          # the contradicted line really ran
    assert not any("non-local" in c.detail for c in checks)


def test_an_lm_studio_setup_is_not_pushed_through_the_ollama_check(ollama, make_file):
    """The tempting fix is to swap in config.LOCAL_MODEL_PREFIXES. It is wrong.

    That tuple answers "is this local?", not "can sift check it?" - so an LM
    Studio model would be looked up in Ollama's inventory, and a user who has
    never installed Ollama would be told to install it before `sift index`
    would run at all. Worse than the misleading line it replaces.
    """
    ollama["models"] = None      # no Ollama on this machine, and none wanted
    make_file("a.md", "x")
    configure(embed_model="lm_studio/bge-small", chat_model="lm_studio/llama-3.1-8b")

    assert not any(c.failed for c in check_models(get_settings()))
    preflight(get_settings(), models=both())    # raises ConfigError under the naive fix


def test_doctor_does_not_promise_local_for_a_default_huggingface_setup(ollama, make_file):
    """`sift doctor` is the report you run *to check this*, and it was wrong.

    Not a doctor bug — check_privacy reads `uses_cloud()` and asked a correct
    question of a bad list — but this is where a user would have caught it, so
    this is where it gets pinned.
    """
    make_file("a.md", "x")
    configure(embed_model="huggingface/BAAI/bge-small-en-v1.5")

    privacy = next(c for c in run_checks(get_settings(), include_index=False)
                   if c.name == "privacy")

    assert privacy.failed
    assert "fully local" not in privacy.detail


def test_every_configured_model_gets_a_line(ollama):
    """A model sift cannot check must still be named.

    Silently dropping it is the other wrong fix: no line reads as "fine", and
    this is the report someone runs precisely when nothing works.
    """
    configure(chat_model="anthropic/claude-sonnet-4-5")   # embed stays on Ollama
    named = " ".join(c.name for c in check_models(get_settings()))
    assert "ollama/nomic-embed-text" in named
    assert "anthropic/claude-sonnet-4-5" in named


@pytest.mark.parametrize("system, phrase", [
    ("Darwin", "brew install ollama"),
    ("Linux", "ollama.com/install.sh"),
    ("Windows", "ollama.com/download"),
])
def test_the_install_hint_matches_the_platform(ollama, monkeypatch, system, phrase):
    ollama["models"] = None
    monkeypatch.setattr(doctor.platform, "system", lambda: system)
    assert phrase in check_models(get_settings())[0].fix


# --- source folder ----------------------------------------------------------

def test_a_folder_with_files_passes_and_counts_them(make_file):
    make_file("a.md", "x")
    make_file("b.txt", "y")
    check = check_source(get_settings())
    assert check.status == OK and "2 indexable file(s)" in check.detail


def test_an_empty_folder_warns_rather_than_fails(source_dir):
    check = check_source(get_settings())
    assert check.status == WARN and not check.failed
    assert ".pdf" in check.fix


def test_a_missing_folder_fails_and_names_the_flag(tmp_path):
    configure(source_dir=tmp_path / "does-not-exist")
    check = check_source(get_settings())
    assert check.failed and "--source" in check.fix


def test_the_source_check_mentions_duplicates_and_skips(make_file):
    make_file("notes.md", "same text")
    make_file("notes (1).md", "same text")
    make_file("movie.mkv", "unsupported")
    detail = check_source(get_settings()).detail
    assert "duplicate" in detail and "skipped" in detail


# --- privacy ----------------------------------------------------------------

def test_an_all_local_setup_says_nothing_leaves():
    check = check_privacy(get_settings())
    assert check.status == OK and "no document text leaves" in check.detail


def test_a_cloud_model_without_consent_is_a_hard_failure():
    configure(chat_model="anthropic/claude-sonnet-4-5", allow_cloud=False)
    check = check_privacy(get_settings())
    assert check.failed and "--allow-cloud" in check.fix


def test_a_cloud_model_with_consent_warns_but_does_not_block():
    configure(chat_model="anthropic/claude-sonnet-4-5", allow_cloud=True)
    check = check_privacy(get_settings())
    assert check.status == WARN and not check.failed
    assert "sent to the provider" in check.detail


# --- index ------------------------------------------------------------------

def test_no_index_yet_warns_and_says_how_to_build_one():
    check = check_index(get_settings())
    assert check.status == WARN and check.fix == "sift index"


def test_a_healthy_index_reports_its_size(embedder, make_file):
    from sift_downloads.index import update_index
    make_file("notes.md", "some text")
    update_index(embedder=embedder)
    check = check_index(get_settings())
    assert check.status == OK and "chunks" in check.detail


def test_a_corrupt_index_fails_with_a_rebuild_instruction(embedder, make_file):
    from sift_downloads.index import update_index
    make_file("notes.md", "some text")
    update_index(embedder=embedder)
    get_settings().index_path.write_bytes(b"garbage")
    check = check_index(get_settings())
    assert check.failed and check.fix


# --- run_checks -------------------------------------------------------------

def test_run_checks_covers_every_area(ollama, make_file):
    make_file("a.md", "x")
    names = " ".join(c.name for c in run_checks(get_settings()))
    for area in ("source folder", "ollama", "privacy", "index"):
        assert area in names


def test_run_checks_can_skip_the_index(ollama, make_file):
    make_file("a.md", "x")
    names = [c.name for c in run_checks(get_settings(), include_index=False)]
    assert "index" not in names


# --- preflight: narrow on purpose ------------------------------------------

def test_preflight_passes_when_everything_is_fine(ollama, make_file):
    make_file("a.md", "x")
    preflight(get_settings(), models=both())          # must not raise


def test_preflight_raises_an_actionable_error_when_ollama_is_down(ollama, make_file):
    make_file("a.md", "x")
    ollama["models"] = None
    with pytest.raises(ConfigError) as e:
        preflight(get_settings(), models=both())
    assert "Try:" in str(e.value) and "sift doctor" in str(e.value)


def test_preflight_can_skip_the_model_check(ollama, make_file):
    """`find` matches filenames, which needs no model at all."""
    make_file("a.md", "x")
    ollama["models"] = None
    preflight(get_settings(), models=both(), require_models=False)   # must not raise


def test_preflight_ignores_warnings(ollama, source_dir):
    """An empty folder is a warning, not a reason to refuse to run."""
    preflight(get_settings(), models=both())          # must not raise


# --- preflight is scoped to the models the command will actually call -------
#
# It used to check both, always. So `sift index` with Ollama for vectors and
# Claude for answers — a sensible split — could not index at all, and was
# refused twice over: once for consent, and once because the chat model was not
# pulled. Neither is loaded by indexing.

def test_indexing_is_not_refused_over_a_chat_model_it_never_calls(ollama, make_file):
    """The finding. Indexing sends nothing to the chat provider."""
    make_file("a.md", "x")
    configure(chat_model="anthropic/claude-sonnet-4-5", allow_cloud=False)
    preflight(get_settings(), models=(get_settings().embed_model,))   # must not raise


def test_indexing_is_not_blocked_by_an_unpulled_chat_model(ollama, make_file):
    """The same defect wearing its other face, and the crueller one.

    Consent at least printed the workaround. This one told the user to download
    an 8GB model before they were allowed to index a folder that never loads it.
    """
    make_file("a.md", "x")
    ollama["models"] = ["nomic-embed-text:latest"]            # chat model absent
    preflight(get_settings(), models=(get_settings().embed_model,))   # must not raise


def test_answering_is_still_refused_over_a_cloud_chat_model(ollama, make_file):
    """The property scoping must not cost. `ask` does call the chat model."""
    make_file("a.md", "x")
    configure(chat_model="anthropic/claude-sonnet-4-5", allow_cloud=False)
    with pytest.raises(ConfigError, match="without consent"):
        preflight(get_settings(), models=both())


def test_doctor_still_reports_a_cloud_model_no_command_is_running(ollama, make_file):
    """A report is not a gate, and narrowing both together is the wrong fix.

    `sift doctor` answers "what is my setup", not "what will this command do".
    Scoping it the same way would drop the cloud chat model from the privacy
    line whenever doctor happened to be asked about indexing — quietly removing
    the one place the user can see the whole picture.
    """
    make_file("a.md", "x")
    configure(chat_model="anthropic/claude-sonnet-4-5", allow_cloud=True)

    privacy = next(c for c in run_checks(get_settings(), include_index=False)
                   if c.name == "privacy")

    assert "anthropic/claude-sonnet-4-5" in privacy.detail


def test_preflight_has_no_default_for_the_models_a_command_uses(ollama, make_file):
    """`models` is required on purpose.

    A default of "both" is what the bug was. A default of "none" is worse — it
    would skip the consent gate for any caller that forgot. Both defaults are
    wrong in a way nobody would notice, so there is no default.
    """
    make_file("a.md", "x")
    with pytest.raises(TypeError):
        preflight(get_settings())


def test_preflight_blocks_an_unconsented_cloud_model(ollama, make_file):
    make_file("a.md", "x")
    configure(chat_model="anthropic/claude-sonnet-4-5", allow_cloud=False)
    with pytest.raises(ConfigError, match="without consent"):
        preflight(get_settings(), models=both(), require_models=False)


# --- the Ollama probe itself -----------------------------------------------

def test_an_unreachable_ollama_reads_as_not_running(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(doctor.urllib.request, "urlopen", boom)
    assert doctor._installed_ollama_models() is None


def test_the_ollama_base_url_can_be_overridden(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_BASE", "http://elsewhere:1234/")
    assert doctor._ollama_base() == "http://elsewhere:1234"


def test_the_ollama_base_url_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)
    assert doctor._ollama_base() == "http://localhost:11434"
