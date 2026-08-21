"""
The interactive session's terminal half.

Two things are tested here that the old ui.read_line never was. The pure pieces
- the rich->ANSI bridge and the live region - are plain objects with no
Application in sight. The Application itself is driven headlessly with
prompt_toolkit's own create_pipe_input/DummyOutput, so key bindings are real
tests rather than a coverage gap.
"""
from __future__ import annotations

import pytest
from prompt_toolkit.formatted_text import ANSI
from rich.console import Console

from sift_downloads.terminal import Cancelled, LiveRegion, Scrollback

# --- the rich -> ANSI bridge ------------------------------------------------

def test_a_complete_line_is_committed_and_a_partial_one_is_held():
    lines = []
    sb = Scrollback(lines.append)
    sb.write("first\nsec")
    assert lines == ["first"]
    sb.write("ond\n")
    assert lines == ["first", "second"]


def test_a_partial_line_is_committed_on_flush():
    lines = []
    sb = Scrollback(lines.append)
    sb.write("no newline")
    assert lines == []
    sb.flush()
    assert lines == ["no newline"]


def test_flushing_twice_does_not_commit_an_empty_line():
    lines = []
    sb = Scrollback(lines.append)
    sb.write("x")
    sb.flush()
    sb.flush()
    assert lines == ["x"]


def test_write_returns_the_character_count_rich_expects():
    assert Scrollback(lambda line: None).write("hello") == 5


def test_riches_escape_sequences_survive_the_bridge():
    """This is the whole reason approach C exists. Writing rich's output through
    prompt_toolkit's stdout proxy replaces every \\x1b with '?' - the styling
    comes back as literal '?[2m' noise. Rendering to a string does not."""
    lines = []
    console = Console(file=Scrollback(lines.append), force_terminal=True,
                      width=60, highlight=False)
    console.print("  [dim]— reading from —[/dim]")
    assert "\x1b[" in lines[0]
    assert "?[" not in lines[0]


def test_the_bridge_hands_prompt_toolkit_real_styles_not_text():
    lines = []
    console = Console(file=Scrollback(lines.append), force_terminal=True,
                      width=60, highlight=False)
    console.print("  [dim]— reading from —[/dim]")
    fragments = ANSI(lines[0]).__pt_formatted_text__()
    assert any("dim" in style for style, _ in fragments), \
        "the dim survived as a style, not as printable text"
    assert "\x1b" not in "".join(text for _, text in fragments)


def test_the_bridge_is_not_a_tty_so_rich_never_tries_to_animate():
    assert Scrollback(lambda line: None).isatty() is False


def test_a_bare_newline_commits_a_blank_line():
    """`ui.console.print()` with no arguments is how ui.py spaces its output, and
    it arrives here as one newline. Swallowing it would run the answer straight
    into the sources block above it."""
    lines = []
    sb = Scrollback(lines.append)
    sb.write("text\n")
    sb.write("\n")
    sb.write("more\n")
    assert lines == ["text", "", "more"]


# --- the live region --------------------------------------------------------


def a_region(cancelled=False):
    """A LiveRegion with its three collaborators replaced by lists."""
    committed: list[str] = []
    repaints: list[int] = []
    region = LiveRegion(committed.append, lambda: repaints.append(1),
                        lambda: cancelled)
    return region, committed, repaints


def test_a_finished_line_leaves_the_region_for_scrollback():
    region, committed, _ = a_region()
    region.append("Notice period: 60 days\n")
    assert committed == ["  Notice period: 60 days"]
    assert region.tail == ""


def test_an_unfinished_line_stays_in_the_region():
    region, committed, _ = a_region()
    region.append("Security deposit is one hun")
    assert committed == []
    assert region.tail == "Security deposit is one hun"


def test_the_tail_is_committed_when_the_answer_ends():
    region, committed, _ = a_region()
    region.append("the last words")
    region.flush()
    assert committed == ["  the last words"]
    assert region.tail == ""


def test_committed_lines_keep_the_two_space_indent():
    """Same indent Padding gave them at ui.py:252, so the answer still lines up
    under the sources block above it."""
    region, committed, _ = a_region()
    region.append("a\nb\n")
    assert committed == ["  a", "  b"]


def test_a_blank_line_in_the_answer_is_kept():
    region, committed, _ = a_region()
    region.append("para one\n\npara two\n")
    assert committed == ["  para one", "", "  para two"]


def test_an_endless_paragraph_is_committed_before_it_pushes_the_box_away():
    """A model that never emits a newline would otherwise grow the region without
    bound. Cut at a space, which is what wrapping does to you anyway."""
    region, committed, _ = a_region()
    region.append("word " * 80)
    assert committed, "nothing was committed, so the region grew unbounded"
    assert len(region.tail) < LiveRegion.TAIL_MAX_CHARS
    assert not committed[0].endswith("wor"), "cut at a space, not mid-word"


def test_a_paragraph_with_no_spaces_at_all_is_still_committed():
    """`rfind` returns -1 when there is nothing to break on. Treating that as
    "do nothing" lets the region grow forever, which is the exact failure
    TAIL_MAX_CHARS exists to prevent."""
    region, committed, _ = a_region()
    region.append("y" * 400)
    assert committed
    assert len(region.tail) <= LiveRegion.TAIL_MAX_CHARS


def test_a_leading_space_does_not_wedge_the_cut():
    """A space at index 0 removes nothing when cut on, and `rfind` reports "not
    found" as -1. Conflating the two stops the tail shrinking ever again."""
    region, committed, _ = a_region()
    region.append(" " + "y" * 400)
    region.append(" and more text after that")
    assert committed
    assert len(region.tail) <= LiveRegion.TAIL_MAX_CHARS


def test_the_streaming_tail_is_actually_drawn():
    """height() counts rows; only render() puts the text on screen. Dropping the
    tail from render() passes every row-count test in this file."""
    region, _, _ = a_region()
    region.append("Security deposit is one hun")
    assert "Security deposit is one hun" in region.render()


def test_appending_asks_for_a_repaint():
    region, _, repaints = a_region()
    region.append("x")
    assert repaints


def test_a_status_asks_for_a_repaint():
    region, _, repaints = a_region()
    region.show("thinking...")
    assert repaints


def test_appending_after_ctrl_c_stops_the_worker():
    """The only place a worker can notice cancellation is between tokens, and the
    region is the only thing it touches there."""
    region, _, _ = a_region(cancelled=True)
    with pytest.raises(Cancelled):
        region.append("a token")


def test_a_cancelled_region_commits_nothing():
    committed: list[str] = []
    region = LiveRegion(committed.append, lambda: None, lambda: True)
    with pytest.raises(Cancelled):
        region.append("a whole line\n")
    assert committed == []


def test_the_region_is_empty_when_nothing_is_happening():
    region, _, _ = a_region()
    assert region.render() == ""
    assert region.height(80) == 0


def test_the_status_line_carries_a_spinner_and_the_seconds():
    region, _, _ = a_region()
    region.show("thinking...")
    out = region.render()
    assert "thinking..." in out
    assert any(frame in out for frame in LiveRegion.FRAMES)
    assert "0s" in out, "the elapsed counter reads 4s, not 0:00:04"


def test_clearing_the_status_empties_the_region():
    region, _, _ = a_region()
    region.show("thinking...")
    region.show("")
    assert region.render() == ""


def test_the_region_is_one_row_for_a_status_and_two_with_a_tail():
    region, _, _ = a_region()
    region.show("thinking...")
    assert region.height(80) == 1
    region.append("short")
    assert region.height(80) == 2


def test_a_wrapped_tail_is_counted_in_rows_not_lines():
    """The Application is inline, so it has to be told its own height. Counting
    the tail as one row would draw the box over the answer."""
    region, _, _ = a_region()
    region.append("x" * 100)
    assert region.height(40) == 3


def test_the_spinner_advances_between_repaints():
    region, _, _ = a_region()
    region.show("thinking...")
    first = region.render()
    region.tick()
    assert region.render() != first
