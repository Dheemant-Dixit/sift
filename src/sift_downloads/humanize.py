"""
Turning values into the short forms a person reads.

Shared because both front ends need them and neither owns them: `cli.py`
prints a result list, `ui.py` draws one, and a size rendered two different
ways in the two places is a bug nobody notices. Nothing here knows which
front end is calling — that is the point, and it is why this module imports
nothing from either of them.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# How a file matched, in the width the result line has room for. `find.py`
# reports content and filename scores separately because "the name matched"
# and "the contents matched" mean different things to the person reading.
MATCH_MARKER = {"content": "·", "filename": "name", "both": "·+name"}


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    # "GB" is the last unit, so that iteration always returns and the loop has
    # no fall-through. A petabyte prints as a large number of GB rather than
    # running off the end.
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def human_age(timestamp: float) -> str:
    delta = datetime.now() - datetime.fromtimestamp(timestamp)
    # A file can legitimately carry a future mtime — clock skew, a synced
    # folder, an archive unpacked with bad timestamps — and on Windows
    # datetime.now() can read a hair behind time.time(). Either way the
    # arithmetic below would print "-1d ago", so treat the future as now.
    if delta.total_seconds() < 0:
        delta = timedelta(0)
    days = delta.days
    if days == 0:
        hours = delta.seconds // 3600
        return "just now" if hours == 0 else f"{hours}h ago"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"
