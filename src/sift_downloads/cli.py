"""
The command line.

Presentation lives here and only here. Every module underneath returns data —
`Answer`, `FileHit`, `SyncStats`, `Check` — so that sift is usable as a library
without having to parse text that was formatted for a terminal.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from sift_downloads import __version__
from sift_downloads.config import ConfigError, configure, get_settings
from sift_downloads.humanize import MATCH_MARKER, human_age, human_size
from sift_downloads.store import IndexProblem

# --- commands --------------------------------------------------------------

def cmd_find(args) -> int:
    from sift_downloads.doctor import preflight
    from sift_downloads.find import find_files
    from sift_downloads.open_file import open_file, reveal_file

    settings = get_settings()
    preflight(settings, need_models=False)  # filename matching works without a model

    if not args.no_sync:
        _quiet_sync(settings)

    hits = find_files(args.query, limit=args.limit, recent_first=args.recent, settings=settings)
    if not hits:
        print(f"Nothing matched {args.query!r} in {settings.source_dir}.")
        print("  Try fewer or more general words, or run `sift status` to see what's indexed.")
        return 1

    for i, hit in enumerate(hits, 1):
        print(f"\n{i:>2}. {hit.name}")
        print(f"    {human_size(hit.size)} · {human_age(hit.modified)} · "
              f"{hit.score:.2f} {MATCH_MARKER[hit.matched_on]}")
        print(f"    {hit.path}")
        if hit.snippet:
            print(f"    “{hit.snippet}”")
        elif hit.note:
            print(f"    ({hit.note})")
        if hit.duplicates:
            print(f"    +{len(hit.duplicates)} identical copy(ies): "
                  f"{', '.join(Path(d).name for d in hit.duplicates)}")

    target = args.open or args.reveal
    if target:
        if not 1 <= target <= len(hits):
            print(f"\nNo result number {target}.", file=sys.stderr)
            return 1
        hit = hits[target - 1]
        print(f"\n{'Revealing' if args.reveal else 'Opening'} {hit.name} ...")
        (reveal_file if args.reveal else open_file)(hit.path)
    return 0


def cmd_ask(args) -> int:
    from sift_downloads.doctor import preflight
    from sift_downloads.generate import answer

    settings = get_settings()
    preflight(settings)
    if not args.no_sync:
        _quiet_sync(settings)

    def respond(question: str) -> None:
        result = answer(question, top_k=args.top_k, min_score=args.min_score, settings=settings)
        print(f"\n{result.text}")
        if result.chunks:
            print("\n— retrieved from —")
            for chunk in result.chunks:
                print(f"  • {chunk['filename']} (chunk {chunk['chunk_index']}, "
                      f"score {chunk['score']:.2f})")

    if args.question:
        respond(" ".join(args.question))
        return 0

    print(f"Ask {settings.source_dir} anything (Ctrl-C or 'quit' to exit).")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if question.lower() in {"quit", "exit", ""}:
            return 0
        respond(question)


def cmd_index(args) -> int:
    from sift_downloads.doctor import preflight
    from sift_downloads.index import Manifest, rebuild_index, update_index

    settings = get_settings()
    preflight(settings)

    logging.getLogger("sift_downloads").setLevel(logging.INFO)
    if args.rebuild:
        print(f"Rebuilding index of {settings.source_dir} from scratch ...")
        stats = rebuild_index(settings)
    else:
        print(f"Syncing index of {settings.source_dir} ...")
        stats = update_index(settings)

    if stats.upgraded:
        print("  index format changed in this version — re-embedded everything once")
    if stats.changed:
        print(f"  +{stats.added} added, ~{stats.modified} modified, -{stats.deleted} deleted")
    else:
        print("  nothing changed")
    print(f"  {stats.chunks_total} chunks across {stats.files_total} files "
          f"({stats.seconds}s)")
    if stats.duplicates:
        print(f"  {stats.duplicates} duplicate file(s) collapsed")
    if stats.skipped:
        print(f"  {stats.skipped} file(s) skipped — see `sift status --skipped`")
    if stats.deferred:
        print(f"  {stats.deferred} file(s) still downloading — run again shortly")

    # Named separately from the general locked count: these are files whose text
    # sift HAD and just lost, because re-embedding cannot recover what only a
    # password made readable.
    for path in stats.needs_unlock:
        print(f"  ! {Path(path).name} was unlocked before — run `sift unlock` to read it again")

    locked = Manifest.load(settings=settings).locked()
    if locked:
        print(f"  {len(locked)} file(s) need a password — run `sift unlock`")
    return 0


def cmd_unlock(args) -> int:
    """Index password-protected PDFs, prompting for each password.

    Separate from `sift index` on purpose: a sync that could block on a prompt
    would be unusable from `sift watch` or a background service.
    """
    from getpass import getpass

    from sift_downloads.doctor import preflight
    from sift_downloads.index import Manifest, unlock_file
    from sift_downloads.ingest import REASON_LOCKED

    settings = get_settings()
    preflight(settings)
    manifest = Manifest.load(settings=settings)

    if args.files:
        # abspath, not resolve(): the index is keyed by the path the scanner
        # produced, and scan_source only expanduser()s the source folder.
        # resolve() also follows symlinks, so through a symlinked source the
        # chunks landed under a key the next scan never yields — which
        # update_index reads as "deleted" and drops. abspath still makes a
        # relative argument absolute, which is the half of resolve() we need.
        targets = [os.path.abspath(Path(f).expanduser()) for f in args.files]
    else:
        targets = manifest.locked()

    if not targets:
        print("No password-protected files in the index — nothing to unlock.")
        return 0

    # Said before anything is asked for. Unlocking trades a protection the user
    # deliberately has for searchability, and that should be their informed call.
    print(f"{len(targets)} file(s) to unlock.\n")
    print("Note: unlocking copies the document's text into sift's index, which")
    print(f"is NOT itself encrypted:\n  {settings.index_path}")
    print("The password is never stored, so `sift index --rebuild` will ask again.")
    print("Press enter with no password to skip a file.\n")

    unlocked = 0
    for target in targets:
        name = Path(target).name
        if not Path(target).exists():
            print(f"  ! {name}: no such file")
            continue

        for attempt in range(3):
            try:
                password = getpass(f"  password for {name}: ")
            except (EOFError, KeyboardInterrupt):
                print("\nStopped.")
                return 1
            if not password:
                print(f"  · {name}: skipped")
                break

            added, reason = unlock_file(target, password, settings=settings)
            if not reason:
                print(f"  ✓ {name}: {added} chunks indexed")
                unlocked += 1
                break
            if reason == REASON_LOCKED:
                remaining = 2 - attempt
                print("  ! wrong password"
                      + (f", {remaining} attempt(s) left" if remaining else ""))
            else:
                # Opened fine, but there was nothing to read — a scanned file
                # that also happened to be locked. No password will fix that.
                print(f"  ! {name}: {reason}")
                break

    print(f"\n{unlocked} file(s) unlocked." if unlocked else "\nNothing unlocked.")
    return 0


def cmd_watch(args) -> int:
    from sift_downloads.doctor import preflight
    from sift_downloads.watch import watch

    settings = get_settings()
    preflight(settings)
    logging.getLogger("sift_downloads").setLevel(logging.INFO)
    watch(settings)
    return 0


def cmd_status(args) -> int:
    from sift_downloads.index import Manifest
    from sift_downloads.store import VectorStore

    settings = get_settings()
    manifest = Manifest.load(settings=settings)

    print(f"source     {settings.source_dir}")
    print(f"index      {settings.data_dir}")
    print(f"models     {settings.embed_model} (embed) · {settings.chat_model} (chat)")
    privacy = ("CLOUD models in use" if settings.uses_cloud()
               else "LOCAL — no document text leaves this machine")
    print(f"privacy    {privacy}")

    if not settings.index_path.exists():
        print("\nNo index yet. Build one with: sift index")
        return 0

    try:
        store = VectorStore.load(settings=settings)
    except IndexProblem as e:
        print(f"\n! {e}")
        return 1

    size_mb = settings.index_path.stat().st_size / 1024 / 1024
    print(f"\n{len(store)} chunks from {len(manifest.files)} files ({size_mb:.1f}MB)")
    if manifest.updated_at:
        print(f"last synced {human_age(manifest.updated_at)}")

    # Grouped by WHY, not lumped together: a locked file needs a password and a
    # scanned one needs OCR, and telling someone the wrong one wastes their time.
    from sift_downloads.ingest import REASON_LOCKED, REASON_NO_TEXT

    by_reason: dict[str, list[str]] = {}
    for path, entry in manifest.files.items():
        if not entry.get("num_chunks"):
            by_reason.setdefault(entry.get("reason") or REASON_NO_TEXT, []).append(path)

    for reason, paths in sorted(by_reason.items()):
        print(f"\n{len(paths)} file(s) — {reason} (findable by name only):")
        for path in sorted(paths)[:10]:
            print(f"  · {Path(path).name}")
        if len(paths) > 10:
            print(f"  ... and {len(paths) - 10} more")
        if reason == REASON_LOCKED:
            print("  → sift unlock     to read these")

    if manifest.duplicates:
        print(f"\n{len(manifest.duplicates)} duplicate file(s) collapsed")

    if args.skipped and manifest.skipped:
        print(f"\n{len(manifest.skipped)} file(s) skipped:")
        for path, reason in sorted(manifest.skipped.items()):
            print(f"  · {Path(path).name} — {reason}")
    elif manifest.skipped:
        print(f"\n{len(manifest.skipped)} file(s) skipped (see --skipped)")
    return 0


def cmd_doctor(args) -> int:
    from sift_downloads.doctor import FAIL, OK, WARN, run_checks

    settings = get_settings()
    symbols = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
    worst_failed = False
    for check in run_checks(settings):
        print(f"[{symbols[check.status]}] {check.name}: {check.detail}")
        if check.fix:
            print(f"         → {check.fix}")
        worst_failed |= check.failed
    return 1 if worst_failed else 0


def cmd_search(args) -> int:
    """Raw chunk-level hits. The tool for calibrating --min-score."""
    from sift_downloads.doctor import preflight
    from sift_downloads.retrieve import search

    settings = get_settings()
    preflight(settings)
    hits = search(args.query, top_k=args.top_k, settings=settings)
    if not hits:
        print("No chunks in the index. Run: sift index")
        return 1
    print(f"\nQuery: {args.query!r}\n")
    for hit in hits:
        preview = " ".join(hit["text"].split())[:180]
        print(f"  #{hit['rank']}  score={hit['score']:.3f}  {hit['filename']} "
              f"[chunk {hit['chunk_index']}]")
        print(f"      {preview}...\n")
    return 0


def cmd_ui(args) -> int:
    from sift_downloads.doctor import preflight
    from sift_downloads.ui import run

    settings = get_settings()
    # need_models=False: the session is still useful with Ollama down — finding
    # files by name needs no model at all, and the failure is reported per-query
    # rather than refusing to start.
    preflight(settings, need_models=False)
    return run(settings)


def cmd_purge(args) -> int:
    from sift_downloads.index import purge_index

    settings = get_settings()
    if not args.yes:
        print(f"This deletes sift's index in {settings.data_dir}.")
        print("Your documents are not touched. Re-run with --yes to confirm.")
        return 1
    removed = purge_index(settings)
    if not removed:
        print("Nothing to delete.")
    for path in removed:
        print(f"deleted {path}")
    return 0


def _quiet_sync(settings) -> None:
    """Bring the index up to date before a query, without narrating it.

    Cheap by design: when nothing changed, the manifest proves it in well under
    a second, so the alternative — answering from a stale index — isn't worth
    the time it saves.
    """
    from sift_downloads.index import update_index

    try:
        update_index(settings)
    except (ConfigError, IndexProblem):
        raise
    except Exception as e:
        print(f"  ! could not refresh the index ({type(e).__name__}: {e}) — "
              f"using what's there", file=sys.stderr)


# --- argument parsing ------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sift",
        description="Ask your Downloads folder where a file is — and what it says.",
        epilog="Everything runs locally by default. Run `sift doctor` if something looks wrong.",
    )
    parser.add_argument("--version", action="version", version=f"sift {__version__}")

    # Settings flags shared by every subcommand. Defaults are None so that
    # "not passed" is distinguishable from "passed the default value" — only
    # what the user actually typed is allowed to override the environment.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--source", type=Path, metavar="DIR",
                        help="folder to search (default: your Downloads)")
    common.add_argument("--data-dir", type=Path, metavar="DIR",
                        help="where to keep the index")
    common.add_argument("--embed-model", metavar="MODEL")
    common.add_argument("--chat-model", metavar="MODEL")
    common.add_argument("--chunk-size", type=int, metavar="N")
    common.add_argument("--chunk-overlap", type=int, metavar="N")
    common.add_argument("--max-file-mb", type=int, metavar="N")
    common.add_argument("--allow-cloud", action="store_true", default=None,
                        help="permit non-local models (sends document text to the provider)")
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("-q", "--quiet", action="store_true")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    p_find = subparsers.add_parser(
        "find", parents=[common], help="find files by what they contain or are called",
        description="Rank files by content and filename. Works on files whose text "
                    "couldn't be extracted, too — those match by name.")
    p_find.add_argument("query", help="what you're looking for")
    p_find.add_argument("-n", "--limit", type=int, default=10, help="how many results (default 10)")
    p_find.add_argument("--recent", action="store_true",
                        help="favor recently modified files")
    p_find.add_argument("--open", type=int, metavar="N", help="open result N")
    p_find.add_argument("--reveal", type=int, metavar="N", help="show result N in the file manager")
    p_find.add_argument("--no-sync", action="store_true", help="skip the pre-query index refresh")
    p_find.set_defaults(func=cmd_find)

    p_ask = subparsers.add_parser(
        "ask", parents=[common], help="ask a question answered from your files",
        description="Retrieve the most relevant passages and answer from them only. "
                    "If nothing is relevant enough, no model is called at all.")
    p_ask.add_argument("question", nargs="*", help="your question (omit for interactive mode)")
    p_ask.add_argument("--top-k", type=int, help="passages to consider")
    p_ask.add_argument("--min-score", type=float, help="relevance bar (default 0.55)")
    p_ask.add_argument("--no-sync", action="store_true", help="skip the pre-query index refresh")
    p_ask.set_defaults(func=cmd_ask)

    p_index = subparsers.add_parser("index", parents=[common], help="build or refresh the index")
    p_index.add_argument("--rebuild", action="store_true",
                         help="re-embed everything from scratch (needed after changing models)")
    p_index.set_defaults(func=cmd_index)

    p_watch = subparsers.add_parser("watch", parents=[common],
                                    help="keep the index fresh as the folder changes")
    p_watch.set_defaults(func=cmd_watch)

    p_status = subparsers.add_parser("status", parents=[common], help="what's indexed right now")
    p_status.add_argument("--skipped", action="store_true", help="list every skipped file")
    p_status.set_defaults(func=cmd_status)

    p_unlock = subparsers.add_parser(
        "unlock", parents=[common], help="index password-protected PDFs",
        description="Prompts for the password of each locked file and indexes "
                    "its text. The password is never stored, so a --rebuild "
                    "will ask again.")
    p_unlock.add_argument("files", nargs="*",
                          help="specific files (default: every locked file)")
    p_unlock.set_defaults(func=cmd_unlock)

    p_doctor = subparsers.add_parser("doctor", parents=[common],
                                     help="check the setup and say how to fix it")
    p_doctor.set_defaults(func=cmd_doctor)

    p_search = subparsers.add_parser(
        "search", parents=[common], help="raw passage hits with scores (for tuning)",
        description="Shows the unfiltered retrieval scores. Use it to calibrate "
                    "--min-score for your own documents — see the README.")
    p_search.add_argument("query")
    p_search.add_argument("--top-k", type=int, default=10)
    p_search.set_defaults(func=cmd_search)

    p_purge = subparsers.add_parser("purge", parents=[common],
                                    help="delete the index (your files are untouched)")
    p_purge.add_argument("--yes", action="store_true", help="confirm")
    p_purge.set_defaults(func=cmd_purge)

    p_ui = subparsers.add_parser(
        "ui", parents=[common], help="interactive session (also: just run `sift`)",
        description="Search and ask in one place, with results scrolling into "
                    "your terminal history and a prompt pinned at the bottom.")
    p_ui.set_defaults(func=cmd_ui)

    return parser


def with_default_command(argv: list[str], interactive: bool = True) -> list[str]:
    """Make a bare `sift` (or `sift --source ...`) start the interactive session.

    Only a leading FLAG is treated as "no command given". A leading word that
    isn't a command stays untouched, so a typo gets argparse's list of valid
    choices instead of a baffling error from inside `ui`.

    `interactive` is False when stdin isn't a terminal. A bare `sift` in a
    script or a pipe then falls through to the help text, rather than starting
    a session with nobody to type into it.
    """
    if argv and argv[0] in ("-h", "--help", "--version"):
        return argv
    if argv and not argv[0].startswith("-"):
        return argv
    if not interactive:
        return argv
    return ["ui", *argv]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(with_default_command(argv, sys.stdin.isatty()))

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    level = logging.WARNING
    if getattr(args, "verbose", False):
        level = logging.DEBUG
    elif getattr(args, "quiet", False):
        level = logging.ERROR
    logging.basicConfig(level=level, format="%(message)s")

    try:
        configure(
            source_dir=args.source,
            data_dir=args.data_dir,
            embed_model=args.embed_model,
            chat_model=args.chat_model,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
            max_file_mb=args.max_file_mb,
            allow_cloud=args.allow_cloud,
        )
        return args.func(args)
    except (ConfigError, IndexProblem) as e:
        print(f"\n{e}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
