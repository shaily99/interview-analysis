"""One JSON file on disk, read once and rewritten atomically on every change.

The quote, theme and timing stores each grew their own copy of this. It is
pulled out here for the stores written since, so that the two properties they
all depend on live in one place:

Writes are atomic. A temp file in the same directory is fsynced and renamed over
the original, so a crash mid-write leaves the old file rather than half a new one.

An unreadable file is never overwritten. The store starts empty, and only when it
is about to write does it move the file aside to ``<name>.corrupt``, so whatever
was in it is still there to recover. Reading alone never moves it: in a synced
study the file may be another coder's, half-synced, and renaming it would delete
it from their folder.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JsonStore:
    """Base for a store backed by one JSON object.

    Subclasses provide ``_empty`` and may override ``_repair`` to fill in keys a
    file written by an older version lacks. Anything the subclass does not know
    about is kept, so a later version's fields survive a round trip.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        #: True when the file exists but could not be read; see _set_aside.
        self.unreadable = False
        self._data = self._load()

    def _empty(self) -> dict:
        raise NotImplementedError

    def _repair(self, data: dict) -> dict:
        return data

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.unreadable = True
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        return self._repair(data)

    def _set_aside(self) -> None:
        """Before overwriting a file that could not be read, keep it as ``.corrupt``."""
        if self.unreadable:
            try:
                os.replace(self.path, self.path.with_suffix(self.path.suffix + ".corrupt"))
            except OSError:
                pass
            self.unreadable = False

    def _write(self) -> None:
        self._set_aside()
        self._data["updated_at"] = now()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            prefix=self.path.name + ".",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(self._data, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
