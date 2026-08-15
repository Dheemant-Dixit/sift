"""
sift — ask your Downloads folder where a file is, and what it says.

Usable as a library as well as a CLI:

    from sift_downloads import find_files, answer, update_index

    update_index()                       # sync the index with the folder
    hits = find_files("rental agreement")  # ranked files
    result = answer("what is my notice period?")
    print(result.text, result.sources)
"""
import os

# Keep litellm offline. On import it otherwise downloads a price list of every
# known model from raw.githubusercontent.com — harmless in content, but it
# makes "nothing leaves your machine" untrue, and sift never reads that list
# because it does no cost accounting. The copy bundled in the litellm package
# is used instead.
#
# This has to happen here. `import litellm` sits above the config import inside
# index.py (third-party before first-party), so setting it in config.py would
# be too late; a package's __init__ always runs before its submodules.
# setdefault, so anyone who genuinely wants the fetch can still ask for it.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from sift_downloads.config import Settings, configure, get_settings  # noqa: E402

__version__ = "0.1.1"

__all__ = [
    "Settings", "configure", "get_settings",
    "find_files", "FileHit", "answer", "Answer",
    "update_index", "rebuild_index", "search", "__version__",
]


def __getattr__(name: str):
    """Expose the heavy modules lazily.

    Importing `sift` should not pull in litellm, numpy and pypdf — the CLI's
    --help would take a second to print. These resolve on first actual use.
    """
    if name in ("find_files", "FileHit"):
        from sift_downloads import find
        return getattr(find, name)
    if name in ("answer", "Answer"):
        from sift_downloads import generate
        return getattr(generate, name)
    if name in ("update_index", "rebuild_index"):
        from sift_downloads import index
        return getattr(index, name)
    if name == "search":
        from sift_downloads import retrieve
        return retrieve.search
    raise AttributeError(f"module 'sift' has no attribute {name!r}")
