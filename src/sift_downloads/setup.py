"""
Getting a stranger from `pip install` to a working sift.

doctor.py observes; this acts. It reads the same two facts doctor reads — is
Ollama running, and does it have the models sift is configured to call — and
then pulls the missing ones through Ollama's own `/api/pull`.

Two lines are drawn on purpose.

**It pulls models; it never installs Ollama.** Pulling into a server the user
already chose to run is reversible and stays inside Ollama's store. `brew
install` and `curl | sh` write to their machine, and doing that would make the
README's "sift never writes to your system" claim false. When there is no
server, setup says how to install one and stops.

**It only touches models Ollama can answer for.** `/api/tags` is the one
inventory sift can read, so an LM Studio or cloud model is *named and skipped*
rather than passed over quietly — a model with no line at all reads as fine,
which is the mistake `check_models` was fixed for.

Nothing here prints. cli.py and ui.py draw a `SetupPlan` and a stream of
`PullProgress` their own way, which is also what lets every path below be
tested with no terminal and no server.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from sift_downloads import doctor
from sift_downloads.config import Settings, get_settings

# Generous, because it bounds the gap between two chunks of a multi-gigabyte
# download rather than the download itself. doctor's 3s is for one small GET.
PULL_TIMEOUT_SECONDS = 60


class SetupError(RuntimeError):
    """A pull that could not finish. Carries a sentence fit to print."""


@dataclass
class SetupPlan:
    """What setup would do, and what it cannot do anything about."""

    to_pull: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    server_up: bool = True
    install_hint: str = ""

    @property
    def ready(self) -> bool:
        """Is there nothing left for setup to do?

        Deliberately not "is the server up". A cloud-only configuration asks
        Ollama for nothing, so a server that is down is none of its business —
        and an offer to fix it would be a false alarm on a working machine.
        """
        return not self.to_pull


@dataclass
class PullProgress:
    """One update from a running pull. Bytes are totals across every layer."""

    model: str
    status: str
    completed: int
    total: int


def plan_setup(settings: Settings | None = None) -> SetupPlan:
    """Which configured models are missing, and which sift cannot fetch.

    With the server down nothing is installed, so every Ollama model is listed
    as missing rather than as unknown: that is the list the user needs in order
    to decide whether installing Ollama is worth it.
    """
    settings = settings or get_settings()

    wanted: list[str] = []
    skipped: list[tuple[str, str]] = []
    # dict.fromkeys keeps the order and drops the repeat, so one model doing
    # both jobs is pulled once.
    for model in dict.fromkeys((settings.embed_model, settings.chat_model)):
        if model.startswith(doctor.OLLAMA_PREFIXES):
            wanted.append(model)
        else:
            skipped.append((model, "sift can only pull Ollama models — install this one yourself"))

    if not wanted:
        return SetupPlan(skipped=skipped)

    installed = doctor._installed_ollama_models()
    if installed is None:
        return SetupPlan(to_pull=wanted, skipped=skipped, server_up=False,
                         install_hint=doctor.ollama_install_hint())
    return SetupPlan(to_pull=[m for m in wanted if not doctor._model_present(installed, m)],
                     skipped=skipped)


def _pull_stream(name: str) -> Iterator[dict]:
    """The one place this module talks to Ollama. Yields the NDJSON events."""
    request = urllib.request.Request(
        f"{doctor._ollama_base()}/api/pull",
        data=json.dumps({"model": name, "stream": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=PULL_TIMEOUT_SECONDS) as resp:
            for line in resp:
                if line.strip():
                    yield json.loads(line)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        raise SetupError(f"lost the connection to Ollama at {doctor._ollama_base()}: {e}") from e


def pull_model(model: str, on_progress: Callable[[PullProgress], None]) -> None:
    """Download one model, reporting bytes as they arrive.

    Ollama streams a line per layer per update, so `completed` and `total` are
    summed across the layers seen so far — a layer reporting again replaces its
    own count. `total` therefore climbs as new layers are announced, which is
    honest: sift genuinely does not know the full size until Ollama says so,
    and a hardcoded number would rot the next time a default model changed.

    A stream that stops before Ollama says `success` is a failure, not a quiet
    return. Half a model on disk reported as done is exactly the shape of bug
    the next command would inherit with no idea where it came from.
    """
    layers: dict[str, tuple[int, int]] = {}
    finished = False

    for event in _pull_stream(doctor._bare_model_name(model)):
        if event.get("error"):
            raise SetupError(f"could not pull {model}: {event['error']}")
        if digest := event.get("digest"):
            layers[digest] = (int(event.get("completed") or 0), int(event.get("total") or 0))
        finished |= event.get("status") == "success"
        on_progress(PullProgress(
            model=model,
            status=str(event.get("status", "")),
            completed=sum(done for done, _ in layers.values()),
            total=sum(size for _, size in layers.values()),
        ))

    if not finished:
        raise SetupError(f"the download of {model} stopped before it finished — "
                         f"re-run `sift setup` and Ollama will resume it")
