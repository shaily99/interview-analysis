"""One JSON file on disk, read once and rewritten atomically on every change.

The quote, theme and timing stores each grew their own copy of this. It is
pulled out here for the stores written since, so that the two properties they
all depend on live in one place:

Writes are atomic. A temp file in the same directory is fsynced and renamed over
the original, so a crash mid-write leaves the old file rather than half a new one.

An unreadable file is never overwritten. It is moved aside to ``<name>.corrupt``
and the store starts empty, so whatever was in it is still there to recover.
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
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        return self._repair(data)

    def _write(self) -> None:
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
