"""
Opening a file, or showing it in the file manager.

Small, but it is what closes the loop. Printing a path leaves you copying it
into a terminal; `--open 2` just opens the thing you were looking for.
"""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path


def open_file(path: Path) -> None:
    """Open a file with the system's default application."""
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["open", str(path)], check=True)
    elif system == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]  # Windows-only
    else:
        subprocess.run(["xdg-open", str(path)], check=True)


def reveal_file(path: Path) -> None:
    """Show a file in the file manager, selected, rather than opening it.

    Usually what you want for a download: you're about to move or rename it, not
    read it. Linux has no standard "select this file" call, so it falls back to
    opening the containing folder.
    """
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["open", "-R", str(path)], check=True)
    elif system == "Windows":
        subprocess.run(["explorer", f"/select,{path}"], check=False)  # returns 1 on success
    else:
        subprocess.run(["xdg-open", str(path.parent)], check=True)
