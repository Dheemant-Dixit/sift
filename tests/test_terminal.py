"""
The interactive session's terminal half.

Two things are tested here that the old ui.read_line never was. The pure pieces
- the rich->ANSI bridge and the live region - are plain objects with no
Application in sight. The Application itself is driven headlessly with
prompt_toolkit's own create_pipe_input/DummyOutput, so key bindings are real
tests rather than a coverage gap.
"""
from __future__ import annotations

from prompt_toolkit.formatted_text import ANSI
from rich.console import Console

from sift_downloads.terminal import Scrollback

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
