"""
sift — ask your Downloads folder where a file is, and what it says.

Usable as a library as well as a CLI:

    from sift import find_files, answer, update_index

    update_index()                       # sync the index with the folder
    hits = find_files("rental agreement")  # ranked files
    result = answer("what is my notice period?")
    print(result.text, result.sources)
"""
from sift.config import Settings, configure, get_settings

__version__ = "0.1.0"

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
        from sift import find
        return getattr(find, name)
    if name in ("answer", "Answer"):
        from sift import generate
        return getattr(generate, name)
    if name in ("update_index", "rebuild_index"):
        from sift import index
        return getattr(index, name)
    if name == "search":
        from sift import retrieve
        return retrieve.search
    raise AttributeError(f"module 'sift' has no attribute {name!r}")
