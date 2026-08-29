"""
Every tunable in one place, resolved at call time rather than import time.

WHY THIS LOOKS THE WAY IT DOES
------------------------------
The obvious way to write config is module-level constants:

    SOURCE_DIR = Path.home() / "Downloads"
    TOP_K = 5

...and then bind them in default arguments (`def search(q, top_k=TOP_K)`).
That reads nicely, and it is a trap: the default is captured when the module is
first imported, so nothing downstream can ever override it. A `--top-k` flag
would silently do nothing.

So instead there is one frozen `Settings` object, built once per process from
CLI flags / environment / .env / defaults, and every function that needs a knob
asks for it at call time (`top_k=None` → fall back to settings). `configure()`
rebuilds it and busts the caches, which is what makes both the CLI flags and
the tests work.

Precedence, highest first:
    1. explicit keyword passed to configure()  (i.e. a CLI flag)
    2. SIFT_* environment variable
    3. .env file in the current directory
    4. the default in this file
"""
from __future__ import annotations

import functools
import os
import platform
import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

# Load .env once, at import. Values already in the real environment win, which
# is what keeps `SIFT_SOURCE=... sift find` overriding a stale .env.
load_dotenv(override=False)

APP_NAME = "sift"

# What to type after `pip install`. NOT the same string as APP_NAME, and the
# difference matters: `sift` was already taken on PyPI, so the distribution is
# `sift-downloads` while the command, the data directory and the import package
# stay `sift`. Anything telling a user how to install something therefore has to
# say `sift-downloads` — naming `sift` there points them at a stranger's project.
DIST_NAME = "sift-downloads"

# File types we know how to extract text from. Everything else in the folder
# (.dmg, .zip, .jpg, installers...) is skipped during ingestion — though `find`
# can still locate those files by name, see find.py.
DEFAULT_EXTENSIONS = frozenset({".pdf", ".md", ".txt", ".docx"})

# Model prefixes whose traffic stays on this machine. Everything NOT matching
# one of these is a cloud model: it would send your document text to a third
# party, so it needs an explicit opt-in. (A prefix check on the litellm model
# string, e.g. "anthropic/claude-…".)
#
# Membership answers exactly one question: with no extra configuration, where
# does litellm send the request? Checked by running each one, not by reading:
#
#   ollama/, ollama_chat/  -> http://localhost:11434
#   lm_studio/             -> nowhere. Raises "Missing API Base" until you set
#                             LM_STUDIO_API_BASE yourself, so sift never picks
#                             the destination.
#   local/                 -> nowhere. Not a litellm provider at all.
#
# `huggingface/` was on this list and does not belong here unconditionally,
# which is the whole reason the list is now two tables. See below.
LOCAL_MODEL_PREFIXES = ("ollama/", "ollama_chat/", "local/", "lm_studio/")

# Prefixes that reach a third party UNLESS the user supplies a base URL, mapped
# to the environment variables litellm reads for it (in the order it reads
# them). For these, locality cannot be read off the model string at all.
#
# litellm's huggingface provider falls back to https://router.huggingface.co
# when no api_base is set, and sift sets none — `index.py` calls
# `litellm.embedding(model=..., input=batch)` bare. So `huggingface/` on the
# plain local list meant a folder of bank statements was POSTed to Hugging Face
# while `uses_cloud()` returned False: no consent gate, no warning, and `sift
# doctor` printing "fully local — no document text leaves this machine".
SELF_HOSTABLE_PREFIXES = {
    "huggingface/": ("HF_API_BASE", "HUGGINGFACE_API_BASE"),
}


def model_is_local(model: str) -> bool:
    """Would this model keep the text on this machine?

    Deliberately a function and not a prefix tuple. For some providers the
    answer is not a property of the model string: `huggingface/bge-small` is
    local with HF_API_BASE set and a third-party upload without it, and litellm
    decides that at call time. A tuple cannot express that, and the version
    that tried leaked every document in the folder.

    The environment is read here rather than frozen into Settings for the same
    reason every other knob is read late — see this module's docstring.

    An env var set to the empty string counts as unset, so a half-configured
    setup is treated as cloud and hits the consent gate. Wrong in the safe
    direction: the cost is one unnecessary --allow-cloud, not a silent upload.
    """
    for prefix, env_vars in SELF_HOSTABLE_PREFIXES.items():
        if model.startswith(prefix):
            return any(os.environ.get(var) for var in env_vars)
    return model.startswith(LOCAL_MODEL_PREFIXES)


# ---------------------------------------------------------------------------
# Locating the user's Downloads folder
# ---------------------------------------------------------------------------

def _windows_downloads() -> Path | None:
    """Ask Windows where Downloads actually is.

    It is NOT reliably %USERPROFILE%\\Downloads — users relocate it, and OneDrive
    redirects it. FOLDERID_Downloads is the only correct answer.
    """
    try:
        import ctypes
        from ctypes import windll, wintypes
        from uuid import UUID

        class GUID(ctypes.Structure):
            _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                        ("d3", wintypes.WORD), ("d4", ctypes.c_byte * 8)]

            def __init__(self, uuid_str):
                super().__init__()
                u = UUID(uuid_str)
                # u.fields[3:] is the same trailing 8 bytes that the loop below
                # copies one at a time; it was being unpacked into a name nothing
                # read. ctypes wants them written into the array, not bound.
                self.d1, self.d2, self.d3 = u.fields[0], u.fields[1], u.fields[2]
                for i, b in enumerate(u.bytes[8:]):
                    self.d4[i] = b

        folderid_downloads = GUID("{374DE290-123F-4565-9164-39C4925E467B}")
        buf = ctypes.c_wchar_p()
        if windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folderid_downloads), 0, None, ctypes.byref(buf)
        ) == 0:
            path = Path(buf.value)
            windll.ole32.CoTaskMemFree(buf)
            return path
    except Exception:
        pass
    return None


def _xdg_downloads() -> Path | None:
    """Read XDG_DOWNLOAD_DIR out of the user's user-dirs.dirs.

    This is what makes localized desktops work: on a French install the folder
    is literally named `Téléchargements`, and hardcoding "~/Downloads" finds
    nothing at all. The file's lines look like:

        XDG_DOWNLOAD_DIR="$HOME/Téléchargements"
    """
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    user_dirs = config_home / "user-dirs.dirs"
    try:
        text = user_dirs.read_text(encoding="utf-8")
    except OSError:
        return None

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("XDG_DOWNLOAD_DIR"):
            continue
        _, _, raw = line.partition("=")
        try:
            value = shlex.split(raw)[0]  # strips the quotes
        except (ValueError, IndexError):
            return None
        # The file uses a literal "$HOME" rather than an expanded path.
        value = value.replace("$HOME", str(Path.home()))
        return Path(os.path.expandvars(value)).expanduser()
    return None


def default_downloads_dir() -> Path:
    """Best guess at this user's Downloads folder, per platform."""
    system = platform.system()
    if system == "Windows":
        found = _windows_downloads()
        if found:
            return found
        return Path(os.environ.get("USERPROFILE", Path.home())) / "Downloads"
    if system == "Linux":
        found = _xdg_downloads()
        if found:
            return found
    return Path.home() / "Downloads"


def default_data_dir() -> Path:
    """Where the index lives — a per-user data directory, never the install dir.

    A pip-installed package must not write next to its own source (that could be
    site-packages, or read-only). Each OS has a blessed spot for this.
    """
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    """One immutable bundle of every knob. Built by `configure()`."""

    source_dir: Path = field(default_factory=default_downloads_dir)
    data_dir: Path = field(default_factory=default_data_dir)

    # Models, addressed through litellm so the same code targets local or cloud.
    embed_model: str = "ollama/nomic-embed-text"
    chat_model: str = "ollama_chat/llama3.1:8b"

    # Chunking. `chunk_size` is the window the MODEL is handed; `child_size` is
    # the smaller window that gets EMBEDDED. See chunk.py for why they differ.
    chunk_size: int = 1000       # target characters per served window
    chunk_overlap: int = 150     # characters shared between adjacent windows
    child_size: int = 300        # max characters per indexed unit; 0 = index whole windows
    child_min: int = 200         # floor on an indexed unit, so it keeps its subject

    # Characters of each document's opening carried alongside its passages, so a
    # passage can be attributed to the right party. 0 disables it.
    doc_head_chars: int = 120

    # Retrieval
    top_k: int = 5               # how many chunks `ask` feeds the model

    # The grounding bar for `ask`. Calibrated for nomic-embed-text: in-domain
    # answers bottom out around 0.56-0.61, clearly-irrelevant queries top out
    # around 0.47-0.51. It is a blunt instrument and it is MODEL-SPECIFIC — see
    # the calibration section of the README before trusting this number with a
    # different embedding model.
    min_score: float = 0.55

    # The candidate bar for `find`. Deliberately looser: `find` is showing you a
    # ranked list of files to choose from, so a marginal hit at position 6 costs
    # you a glance. `ask` is putting text in front of an LLM as fact, so a
    # marginal hit there costs you a wrong answer.
    find_min_score: float = 0.40

    # How much better a second passage from a file `ask` is already reading has
    # to score than the best passage of a file it is not reading, to be worth
    # the slot. A cosine gap, so it is MODEL-SPECIFIC in the same way
    # `min_score` is. 0 means pure score order — one file may take every slot,
    # which is the crowding defect. A large value means one passage per file,
    # which cuts "summarise this book" to a single passage.
    #
    # 0.02 was measured, not chosen. The question it has to get right is
    # decided by a 0.003 gap between a redundant passage and the answer, and
    # genuine depth in one document runs 0.02-0.04 ahead of the next file. Any
    # value in 0.01-0.04 separates those two on the folder it was measured on;
    # 0.05 starts taking slots away from documents that had earned them.
    repeat_margin: float = 0.02

    # Ingestion limits
    max_file_mb: int = 50
    extensions: frozenset[str] = DEFAULT_EXTENSIONS

    # The lexical presence gate: refuse a question whose words appear in no
    # indexed passage, before anything is embedded. An escape hatch rather than
    # a tuning knob — the rule that decides which words may veto is fitted to a
    # single observed failure, so a wrong refusal needs a way out that is not a
    # downgrade. See generate.absent_terms.
    lexical_gate: bool = True

    # Privacy: cloud models require an explicit opt-in, never inferred.
    allow_cloud: bool = False

    @property
    def index_path(self) -> Path:
        return self.data_dir / "index.npz"

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.json"

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    def uses_cloud(self, models: Sequence[str] | None = None) -> bool:
        """True if any of these models would send text off this machine."""
        return bool(self.cloud_models(models))

    def cloud_models(self, models: Sequence[str] | None = None) -> list[str]:
        """Which of `models` are not local? Defaults to both configured models.

        The default answers "is this configuration cloudy?", which is the right
        question for a *report* — `sift status` and `sift doctor` should name a
        cloud model whether or not the command in front of them will call it.

        A *gate* needs the other question: "will this operation send text to a
        third party?" — and must pass the models it is about to call. Asking the
        configuration-wide question refused `sift index` over a chat model
        indexing never touches, so a local embedder plus a cloud chat model
        could not index at all without --allow-cloud.
        """
        if models is None:
            models = (self.embed_model, self.chat_model)
        return [m for m in models if not model_is_local(m)]


class ConfigError(Exception):
    """Raised for a configuration problem the user can fix, with advice on how."""


# ---------------------------------------------------------------------------
# Reading the environment
# ---------------------------------------------------------------------------

def _env_str(name: str) -> str | None:
    value = os.environ.get(f"SIFT_{name.upper()}")
    return value.strip() if value and value.strip() else None


def _env_int(name: str) -> int | None:
    raw = _env_str(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"SIFT_{name.upper()} must be an integer, got {raw!r}") from None


def _env_float(name: str) -> float | None:
    raw = _env_str(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"SIFT_{name.upper()} must be a number, got {raw!r}") from None


def _env_bool(name: str) -> bool | None:
    raw = _env_str(name)
    if raw is None:
        return None
    return raw.lower() in {"1", "true", "yes", "on"}


def _from_env() -> dict:
    """Every SIFT_* variable that is actually set, as Settings kwargs.

    Each reader returns None when its variable is absent, and absent keys are
    dropped — so an unset variable falls through to the dataclass default
    rather than overwriting it with None.
    """
    raw = {
        "source_dir": _env_str("source"),
        "data_dir": _env_str("data_dir"),
        "embed_model": _env_str("embed_model"),
        "chat_model": _env_str("chat_model"),
        "chunk_size": _env_int("chunk_size"),
        "chunk_overlap": _env_int("chunk_overlap"),
        "child_size": _env_int("child_size"),
        "child_min": _env_int("child_min"),
        "doc_head_chars": _env_int("doc_head_chars"),
        "top_k": _env_int("top_k"),
        "min_score": _env_float("min_score"),
        "find_min_score": _env_float("find_min_score"),
        "repeat_margin": _env_float("repeat_margin"),
        "max_file_mb": _env_int("max_file_mb"),
        "lexical_gate": _env_bool("lexical_gate"),
        "allow_cloud": _env_bool("allow_cloud"),
    }
    values = {k: v for k, v in raw.items() if v is not None}
    for key in ("source_dir", "data_dir"):
        if key in values:
            values[key] = Path(values[key]).expanduser()
    return values


# ---------------------------------------------------------------------------
# The process-wide settings singleton
# ---------------------------------------------------------------------------

_overrides: dict = {}


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The active settings for this process (cached; built on first use)."""
    values = _from_env()
    values.update(_overrides)  # explicit overrides (CLI flags) win over env
    settings = Settings(**values)
    return _validated(settings)


def configure(**overrides) -> Settings:
    """Apply explicit overrides (CLI flags) and rebuild the settings.

    REPLACES the previous overrides rather than merging into them, so a process
    can't accumulate settings from an earlier call it has forgotten about. The
    CLI makes exactly one call, passing everything the user typed.

    Callers pass only what the user actually specified — a None value means
    "not specified", so it never clobbers an env var with a default.
    """
    global _overrides
    _overrides = {k: v for k, v in overrides.items() if v is not None}
    reset_caches()
    return get_settings()


def reset() -> None:
    """Drop all overrides and caches without building new settings.

    Distinct from `configure()` because it cannot fail: teardown code needs to
    restore a clean state even when the thing under test was a configuration
    that raises.
    """
    global _overrides
    _overrides = {}
    reset_caches()


def invalidate_store_cache() -> None:
    """Drop the loaded VectorStore, so the next query re-reads it from disk.

    Two different things stale it, which is why this is separate from
    `reset_caches()`: the settings can change under it (a `--source` flag now
    points at a different index), or the FILE can change under it (a sync just
    rewrote the one it was loaded from). The second has nothing to do with
    settings, so the index layer needs to be able to say "the file moved" without
    also tearing down the settings singleton.

    Looked up through sys.modules rather than imported: if retrieve was never
    loaded there is no cache to clear, and importing it here would drag litellm
    and numpy into commands that don't need them — and index.py, which calls
    this, cannot import retrieve at all, since retrieve imports index.
    """
    retrieve = sys.modules.get("sift_downloads.retrieve")
    if retrieve is not None:
        retrieve.get_store.cache_clear()


def reset_caches() -> None:
    """Drop every cache derived from settings.

    Anything memoized off the old Settings has to go, or a `--source` flag will
    quietly query the previous folder's index. `retrieve.get_store()` is the
    dangerous one.
    """
    get_settings.cache_clear()
    invalidate_store_cache()


def validate_top_k(top_k: int) -> None:
    """Reject a top_k that would silently return the wrong number of chunks.

    `np.argsort(scores)[-k:]` reads `[-0:]` as `[0:]` — the entire index rather
    than none of it — and `[-1:]` as everything but the worst match. Both are
    wrong answers, not errors, so nothing downstream can notice.

    Called from three places because three different things supply a k, and no
    one of them sees the others: the settings (`_validated`), the library entry
    point (`retrieve.search`, which short-circuits on an empty index before the
    store is ever consulted), and the store. `--top-k` reaches none of them
    through `configure()` — it is a per-command flag with a different default
    on each command.
    """
    if top_k < 1:
        raise ConfigError(f"top_k must be at least 1, got {top_k}")


def _validated(settings: Settings) -> Settings:
    """Reject configurations that would fail confusingly later on."""
    if settings.chunk_overlap < 0:
        raise ConfigError(
            f"chunk_overlap ({settings.chunk_overlap}) cannot be negative — "
            f"chunking would advance past each window and never index the gap."
        )
    if settings.chunk_overlap >= settings.chunk_size:
        raise ConfigError(
            f"chunk_overlap ({settings.chunk_overlap}) must be smaller than "
            f"chunk_size ({settings.chunk_size}), or chunking cannot advance."
        )
    validate_top_k(settings.top_k)
    if settings.child_size < 0 or settings.child_min < 0:
        raise ConfigError(
            f"child_size ({settings.child_size}) and child_min ({settings.child_min}) "
            f"cannot be negative."
        )

    # The indexed unit is cut FROM the served window, so it can never be larger
    # than one. Clamping rather than rejecting keeps `--chunk-size 200` from
    # erroring about a knob the user never touched: the relationship is
    # structural, so it should hold by construction.
    child_size = min(settings.child_size, settings.chunk_size)
    return replace(
        settings,
        source_dir=settings.source_dir.expanduser(),
        data_dir=settings.data_dir.expanduser(),
        child_size=child_size,
        child_min=min(settings.child_min, child_size) if child_size else settings.child_min,
    )


def require_source_dir(settings: Settings | None = None) -> Path:
    """Return the source folder, or explain precisely what to do about it.

    Without this, a missing folder surfaces as a bare FileNotFoundError from
    `Path.iterdir()` several layers down — accurate, but useless to a new user
    whose Downloads folder just lives somewhere else.
    """
    settings = settings or get_settings()
    source = settings.source_dir
    if source.is_dir():
        return source

    hint = (
        "Point sift somewhere else with:  sift <command> --source /path/to/folder\n"
        "  or set it permanently:          export SIFT_SOURCE=/path/to/folder"
    )
    if not source.exists():
        raise ConfigError(f"Source folder does not exist: {source}\n  {hint}")
    raise ConfigError(f"Source path is not a folder: {source}\n  {hint}")


def check_cloud_consent(settings: Settings | None = None,
                        models: Sequence[str] | None = None) -> None:
    """Refuse to send document text to a cloud provider without explicit consent.

    A cloud model string alone is NOT consent. Your Downloads folder holds bank
    statements, ID scans and contracts; shipping that to a third party should be
    a decision, not a side effect of editing a model name.

    `models` names the models the operation about to run will actually call.
    Pass it. The default is both configured models, which is a fine answer for a
    report and the wrong one for a gate: consent is about text leaving, and a
    model that is never called sends nothing.
    """
    settings = settings or get_settings()
    cloud = settings.cloud_models(models)
    if not cloud or settings.allow_cloud:
        return
    raise ConfigError(
        f"Refusing to use a non-local model without consent: {', '.join(cloud)}\n"
        f"  This would send the text of your documents to that provider.\n"
        f"{_self_hosting_hint(cloud)}"
        f"  If that is what you want, re-run with --allow-cloud (or set "
        f"SIFT_ALLOW_CLOUD=1)."
    )


def _self_hosting_hint(models: list[str]) -> str:
    """Name the variable that would make a self-hosted setup count as local.

    Only providers in SELF_HOSTABLE_PREFIXES have one. For `anthropic/` there
    is no base URL that keeps the text here, so offering one would be a lie —
    which is why this reads the same table `model_is_local` does rather than
    guessing from the model string.

    This exists because closing the huggingface leak changed behaviour for the
    people running it against their own server: they were silently counted as
    local and now hit this gate. Telling them which knob restores that is the
    difference between a fix and a regression.
    """
    for model in models:
        for prefix, env_vars in SELF_HOSTABLE_PREFIXES.items():
            if model.startswith(prefix):
                return (f"  Running {prefix.rstrip('/')} on your own server? Set "
                        f"{env_vars[0]} and sift will count it as local.\n")
    return ""


# Which cloud models have already announced themselves this process. A set and
# not a bool: the warning is now raised per operation, so a run that embeds with
# one cloud provider and answers with another must announce both. A single latch
# named whichever came first and let the second one leave silently.
_cloud_warned: set[str] = set()


def warn_if_cloud(settings: Settings | None = None,
                  models: Sequence[str] | None = None) -> None:
    """Print a one-time reminder when consent has been given and cloud is in use.

    Takes the same `models` as check_cloud_consent, and for the same reason.
    Disclosure has to track the gate: telling you text is leaving while indexing
    a folder that only touches a local embedder is a false alarm, and the
    warnings people learn to ignore are the ones that fire when nothing is
    happening.
    """
    settings = settings or get_settings()
    unannounced = [m for m in settings.cloud_models(models) if m not in _cloud_warned]
    if not unannounced:
        return
    _cloud_warned.update(unannounced)
    print(
        f"  ! cloud model in use ({', '.join(unannounced)}) — "
        f"document text is leaving this machine",
        file=sys.stderr,
    )
