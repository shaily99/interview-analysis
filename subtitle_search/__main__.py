"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from pathlib import Path

from .session import RecordingError, RecordingRegistry
from .vtt import format_timestamp

DEFAULT_PORT = 8765


def _dump_parse(registry: RecordingRegistry) -> int:
    """Print what the parser made of a folder, without starting the server.

    Speaker detection is heuristic, so this exists to let you sanity check the
    result against a real transcript without the file leaving your machine.
    """
    for recording in registry.list():
        transcript = recording.transcript
        diagnostics = transcript.diagnostics()
        print(f"\n{recording.title}")
        quotes = recording.store.list()
        initials = {c["id"]: c["initials"] for c in registry.coders.list()}
        per_coder = {}
        for quote in quotes:
            who = initials.get(quote["coder"], quote["coder"])
            per_coder[who] = per_coder.get(who, 0) + 1
        split = ", ".join(f"{who} {n}" for who, n in sorted(per_coder.items()))
        print(f"  quotes     : {len(quotes)}" + (f" ({split})" if split else ""))
        print(f"  duration   : {format_timestamp(diagnostics['duration'])}")
        print(f"  cues       : {diagnostics['cue_count']}")
        print(f"  chunks     : {diagnostics['chunk_count']}")
        print(f"  detection  : {diagnostics['speaker_detection']}")

        # The part layout is the thing worth checking on real data: a wrong order
        # or a wrong duration silently shifts every timestamp after it.
        print(f"  recordings : {len(transcript.parts)}")
        for part in transcript.parts:
            started = part.started_at.strftime("%Y-%m-%d %H:%M:%S") if part.started_at else "no timestamp"
            print(
                f"      {part.index + 1}. {part.vtt_name}"
                f"  [{started}]"
            )
            print(
                f"         media {part.media_name or '(none found)'}"
                f"  ·  starts at {format_timestamp(part.offset)}"
                f"  ·  runs {format_timestamp(part.duration)}"
            )
            if part.gap_before is not None:
                print(f"         interruption before this part: {format_timestamp(part.gap_before)}")
        speakers = diagnostics["speakers"]
        print(f"  speakers   : {len(speakers)}")
        for name in speakers:
            spoken = sum(1 for c in transcript.cues if c.speaker == name)
            print(f"      - {name}  ({spoken} cues)")
        if not speakers:
            print("      (none detected -- chunks were split on pauses instead)")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="subtitle-search",
        description="Read Zoom transcripts, find quotes, and scrub to them in the recording.",
    )
    parser.add_argument("folder", type=Path, help="recording folder containing a .vtt and media file")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: localhost only)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="restart the server when the Python source changes (for development)",
    )
    parser.add_argument(
        "--dump-parse",
        action="store_true",
        help="print the parse summary for the folder and exit",
    )
    args = parser.parse_args(argv)

    registry = RecordingRegistry()
    # One folder or a folder of folders -- a transcript sitting in the folder
    # itself means it is the recording, so there is nothing to ask about.
    try:
        registry.add_library(args.folder)
    except RecordingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not registry.list():
        print(f"error: no recordings found in {args.folder}", file=sys.stderr)
        for name, why in registry.failures:
            print(f"  {name}: {why}", file=sys.stderr)
        return 1

    if args.dump_parse:
        return _dump_parse(registry)

    import uvicorn

    from .app import FOLDER_ENV, create_app

    recording = registry.default
    assert recording is not None
    url = f"http://{args.host}:{args.port}/"

    if registry.is_library:
        total = sum(len(r.store.list()) for r in registry.list())
        print(f"  library: {len(registry.list())} recordings, {total} saved quotes")
        for entry in registry.list():
            print(f"    {entry.title}  ({format_timestamp(entry.transcript.duration)})")
    else:
        diagnostics = recording.transcript.diagnostics()
        print(f"  {recording.title}")
        print(f"  {diagnostics['cue_count']} cues, {diagnostics['chunk_count']} chunks, "
              f"{len(recording.transcript.speakers)} speakers")
        if diagnostics["part_count"] > 1:
            print(f"  {diagnostics['part_count']} recordings joined into one "
                  f"{format_timestamp(diagnostics['duration'])} timeline")
        if recording.media_kind is None:
            print("  no media file found -- transcript will be read-only")

    for name, why in registry.failures:
        print(f"  skipped {name}: {why}")
    print(f"\n  {url}\n")

    if args.reload:
        print("  reloading on Python changes\n")

    if not args.no_open:
        # In the parent process, so a reload does not open another tab every time.
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    if args.reload:
        # A reloading server re-imports the app in a fresh process, so it cannot be
        # given one that is already built -- the folder travels in the environment
        # and the child builds its own registry from it.
        #
        # Only this package is watched. The recording folder is deliberately not:
        # quotes and timings are written into it constantly, and a save should not
        # restart the server that just did the saving.
        os.environ[FOLDER_ENV] = str(args.folder.expanduser().resolve())
        uvicorn.run(
            "subtitle_search.app:from_environment",
            factory=True,
            reload=True,
            reload_dirs=[str(Path(__file__).resolve().parent)],
            host=args.host,
            port=args.port,
            # Louder than usual on purpose: a reload you cannot see happening is
            # worse than no reload, because you go on debugging the old code.
            log_level="info",
        )
    else:
        uvicorn.run(create_app(registry), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
