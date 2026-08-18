"""
Preflight checks — turning stack traces into instructions.

The single most likely first-run experience for a stranger is a wall of litellm
connection errors because Ollama isn't installed. That's a solved problem: check
the handful of things that can be wrong, and say what to do about each one.

`sift doctor` runs all of these. `index`, `find` and `ask` run the relevant ones
and stop at the first hard failure, so the error you see is the thing you need
to fix.
"""
from __future__ import annotations

import json
import platform
import urllib.error
import urllib.request
from dataclasses import dataclass

from sift_downloads.config import ConfigError, Settings, get_settings
from sift_downloads.store import IndexProblem, VectorStore

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str          # OK | WARN | FAIL
    detail: str
    fix: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL


def _ollama_base() -> str:
    import os
    base = os.environ.get("OLLAMA_API_BASE") or "http://localhost:11434"
    return base.rstrip("/")


def _installed_ollama_models() -> list[str] | None:
    """Model names Ollama has locally, or None if it isn't reachable."""
    try:
        with urllib.request.urlopen(f"{_ollama_base()}/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None
    return [m.get("name", "") for m in data.get("models", [])]


def _bare_model_name(model: str) -> str:
    """'ollama_chat/llama3.1:8b' -> 'llama3.1:8b'."""
    return model.split("/", 1)[1] if "/" in model else model


def _model_present(installed: list[str], model: str) -> bool:
    """Ollama reports 'nomic-embed-text:latest' for a model pulled as 'nomic-embed-text'."""
    want = _bare_model_name(model)
    return any(name == want or name.split(":")[0] == want.split(":")[0] for name in installed)


def check_models(settings: Settings) -> list[Check]:
    """Is the machinery behind the configured models actually available?"""
    local_models = [m for m in (settings.embed_model, settings.chat_model)
                    if m.startswith(("ollama/", "ollama_chat/"))]
    if not local_models:
        return [Check("models", OK,
                      f"using non-local models: "
                      f"{settings.embed_model}, {settings.chat_model}")]

    installed = _installed_ollama_models()
    if installed is None:
        install_hint = {
            "Darwin": "brew install ollama && brew services start ollama",
            "Linux": "curl -fsSL https://ollama.com/install.sh | sh",
        }.get(platform.system(), "install Ollama from https://ollama.com/download")
        return [Check(
            "ollama", FAIL,
            f"no Ollama server at {_ollama_base()}",
            f"{install_hint}\n     (or point sift at a cloud model — see README)",
        )]

    checks = [Check("ollama", OK, f"running at {_ollama_base()}")]
    for model in local_models:
        if _model_present(installed, model):
            checks.append(Check(f"model {model}", OK, "pulled"))
        else:
            checks.append(Check(
                f"model {model}", FAIL, "not pulled",
                f"ollama pull {_bare_model_name(model)}",
            ))
    return checks


def check_source(settings: Settings) -> Check:
    """Does the folder exist, and is there anything in it worth indexing?"""
    from sift_downloads.ingest import scan_source

    try:
        scan = scan_source(settings)
    except ConfigError as e:
        return Check("source folder", FAIL, str(e).split("\n")[0],
                     "sift <command> --source /path/to/folder")
    except OSError as e:
        return Check("source folder", FAIL, f"cannot read {settings.source_dir}: {e}")

    n = len(scan.files)
    detail = f"{settings.source_dir} — {n} indexable file(s)"
    if scan.duplicates:
        detail += f", {len(scan.duplicates)} duplicate(s) collapsed"
    if scan.skipped:
        detail += f", {len(scan.skipped)} skipped"
    if n == 0:
        return Check("source folder", WARN, detail,
                     f"nothing to index — sift reads {', '.join(sorted(settings.extensions))}")
    return Check("source folder", OK, detail)


def check_index(settings: Settings) -> Check:
    """Is there an index, and was it built by the model we're configured to use?"""
    if not settings.index_path.exists():
        return Check("index", WARN, f"not built yet ({settings.data_dir})", "sift index")
    try:
        store = VectorStore.load(settings=settings)
    except IndexProblem as e:
        first, _, rest = str(e).partition("\n")
        return Check("index", FAIL, first, rest.strip() or "sift index --rebuild")
    size_mb = settings.index_path.stat().st_size / 1024 / 1024
    return Check("index", OK, f"{len(store)} chunks, {size_mb:.1f}MB in {settings.data_dir}")


def check_privacy(settings: Settings) -> Check:
    """Say plainly whether anything leaves the machine."""
    if not settings.uses_cloud():
        return Check("privacy", OK, "fully local — no document text leaves this machine")
    models = ", ".join(settings.cloud_models())
    if settings.allow_cloud:
        return Check("privacy", WARN,
                     f"cloud models in use ({models}) — "
                     f"document text is sent to the provider")
    return Check("privacy", FAIL, f"cloud models configured ({models}) without consent",
                 "re-run with --allow-cloud if that's intended")


def run_checks(settings: Settings | None = None, include_index: bool = True) -> list[Check]:
    settings = settings or get_settings()
    checks = [check_source(settings), *check_models(settings), check_privacy(settings)]
    if include_index:
        checks.append(check_index(settings))
    return checks


def preflight(settings: Settings | None = None, need_models: bool = True) -> None:
    """Raise a clean, actionable error before doing real work.

    Deliberately narrow: it only raises on things that WILL break the command
    about to run. Warnings are left for `sift doctor` to report.
    """
    settings = settings or get_settings()
    checks = [check_source(settings), check_privacy(settings)]
    if need_models:
        checks += check_models(settings)

    for check in checks:
        if check.failed:
            message = f"{check.detail}"
            if check.fix:
                message += f"\n\n  Try:  {check.fix}"
            message += "\n\n  Run `sift doctor` for the full picture."
            raise ConfigError(message)
