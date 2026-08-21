"""
The interactive session's state machine, with no terminal involved.

`Runner` answers three questions — Enter was pressed, a line finished, Ctrl-C was
pressed — and every answer is a plain string. That is the whole point: the
Application becomes a thin caller that reads a key, asks the runner, and does what
it says, so the interesting decisions are testable without a pty.
"""
from __future__ import annotations

from sift_downloads.session import Runner, Verdict

# --- submit -----------------------------------------------------------------

def test_the_first_line_runs_immediately():
    runner = Runner()
    assert runner.submit("notice period") == Verdict.RUN
    assert runner.busy is True


def test_a_blank_line_is_ignored_and_does_not_make_the_runner_busy():
    runner = Runner()
    assert runner.submit("   ") == Verdict.IGNORE
    assert runner.busy is False


def test_a_second_line_queues_rather_than_running_in_parallel():
    runner = Runner()
    runner.submit("first")
    assert runner.submit("second") == Verdict.QUEUE
    assert runner.queued == "second"


def test_a_third_line_is_refused_because_the_queue_holds_one():
    runner = Runner()
    runner.submit("first")
    runner.submit("second")
    assert runner.submit("third") == Verdict.REJECT
    assert runner.queued == "second", "the pending line must not be replaced"


def test_a_blank_line_while_busy_is_ignored_not_queued():
    runner = Runner()
    runner.submit("first")
    assert runner.submit("") == Verdict.IGNORE
    assert runner.queued is None


# --- finished ---------------------------------------------------------------

def test_finishing_with_nothing_queued_leaves_the_runner_idle():
    runner = Runner()
    runner.submit("first")
    assert runner.finished() is None
    assert runner.busy is False


def test_finishing_hands_back_the_queued_line_and_stays_busy():
    runner = Runner()
    runner.submit("first")
    runner.submit("second")
    assert runner.finished() == "second"
    assert runner.busy is True
    assert runner.queued is None


def test_the_queue_holds_one_line_again_once_the_first_is_taken():
    runner = Runner()
    runner.submit("first")
    runner.submit("second")
    runner.finished()
    assert runner.submit("third") == Verdict.QUEUE


def test_going_idle_after_a_cancel_does_not_leave_the_flag_set():
    """`cancelled` is read by the live region between tokens, so a flag left
    standing after the line ends would abort the next one for no reason. The
    queued path is covered by _start(); this is the path that is not."""
    runner = Runner()
    runner.submit("first")
    runner.interrupt("")
    assert runner.finished() is None        # nothing queued: _start() never runs
    assert runner.cancelled is False
    assert runner.should_stop() is False, "ctrl-c must not be sticky the way leaving is"


# --- interrupt --------------------------------------------------------------

def test_ctrl_c_while_working_cancels_and_drops_the_queue():
    runner = Runner()
    runner.submit("first")
    runner.submit("second")
    assert runner.interrupt("") == Verdict.CANCEL
    assert runner.cancelled is True
    assert runner.queued is None, "ctrl-c is the user saying stop, so the queue goes"


def test_ctrl_c_while_working_does_not_free_the_runner():
    """Python cannot kill a thread. The abandoned worker is still holding Ollama,
    so the next line must queue rather than start a second call."""
    runner = Runner()
    runner.submit("first")
    runner.interrupt("")
    assert runner.busy is True
    assert runner.submit("second") == Verdict.QUEUE


def test_ctrl_c_idle_with_text_clears_the_line():
    assert Runner().interrupt("half a question") == Verdict.CLEAR


def test_ctrl_c_idle_and_empty_asks_for_ctrl_d():
    assert Runner().interrupt("") == Verdict.HINT


def test_ctrl_c_idle_on_whitespace_only_clears_rather_than_hinting():
    assert Runner().interrupt("   ") == Verdict.CLEAR


def test_the_cancelled_flag_is_cleared_when_the_next_line_starts():
    runner = Runner()
    runner.submit("first")
    runner.interrupt("")
    runner.submit("second")            # queued behind the abandoned thread
    runner.finished()
    assert runner.cancelled is False, "the new line must not start pre-cancelled"


def test_leaving_drops_the_line_that_was_waiting():
    """Ctrl-D. The worker's own finally is what starts the queued line, and
    nothing can kill that thread - so if the queue survives, sift answers the
    question you abandoned after you have gone."""
    runner = Runner()
    runner.submit("first")
    runner.submit("second")
    assert runner.queued == "second"
    runner.leaving()
    assert runner.queued is None
    assert runner.finished() is None, "the abandoned line was started anyway"


def test_leaving_also_stops_the_line_that_is_running():
    """Same decision as dropping the queue, made in the same keystroke: nobody
    who has left wants the rest of the answer. It is the cooperative flag, so
    it stops the line at its next token, not at once."""
    runner = Runner()
    runner.submit("first")
    runner.leaving()
    assert runner.should_stop() is True


def test_leaving_is_not_undone_by_the_line_finishing():
    """Leaving is terminal, and finished() runs before the worker notices.

    asyncio.run's teardown cancels the pending task; its finally calls
    finished(). The executor thread cannot be cancelled, so it is still
    streaming, and a per-line flag is already back down by the time it reaches
    its next token. That is the whole answer streamed at someone who has gone.
    """
    runner = Runner()
    runner.submit("first")
    runner.leaving()
    runner.finished()                  # teardown gets here before the worker does
    assert runner.should_stop() is True, "finished() let the abandoned answer resume"
    assert runner.left is True
