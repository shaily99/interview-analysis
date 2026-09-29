"""Who is coding.

Several people code the same study from a shared folder, so every quote, span and
code has to say whose it is. Each coder is one folder, ``coders/<id>/`` at the
study root, holding a ``coder.json`` with their name and initials. Their codebooks
live beside it, and their quotes and spans sit in the same-named folder inside
each recording.

A folder per coder is what makes a synced folder safe to share: only a coder's
own tool ever writes into their folder, so two people never write the same file
and the sync client never has two versions to choose between.

There is no password. Access to the study is whatever access to the shared
folder is; the name is there to label work, not to guard it.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from .jsonstore import JsonStore, now

CODERS_DIRNAME = "coders"
CODER_FILENAME = "coder.json"


class CoderError(ValueError):
    """A request that cannot be applied, reported back as a 400."""


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def suggest_initials(name: str, taken=()) -> str:
    """Initials for a name, lengthened from the last name until nobody has them.

    "Shaily Bhatt" gives SB, then SBh, SBha, ... when those are taken. Matching
    ignores case, since SB and Sb would read as the same person on a chip.
    """
    words = [re.sub(r"[^\w]", "", w) for w in _clean(name).split()]
    words = [w for w in words if w]
    if not words:
        return ""
    last = words[-1] if len(words) > 1 else ""
    base = words[0][0].upper() + (last[0].upper() if last else "")
    taken = {t.lower() for t in taken}
    candidate, extra = base, (last or words[0])[1:]
    for letter in extra:
        if candidate.lower() not in taken:
            return candidate
        candidate += letter.lower()
    counter = 2
    while candidate.lower() in taken:
        candidate = f"{base}{counter}"
        counter += 1
    return candidate


class _CoderFile(JsonStore):
    def _empty(self) -> dict:
        return {}


class CoderDirectory:
    """The coders of one study, read from ``<root>/coders/*/coder.json``."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._files: dict[str, _CoderFile] = {}
        self.reload()

    @property
    def folder(self) -> Path:
        return self.root / CODERS_DIRNAME

    def reload(self) -> None:
        self._files = {}
        if not self.folder.is_dir():
            return
        for child in sorted(self.folder.iterdir()):
            path = child / CODER_FILENAME
            if child.is_dir() and path.is_file():
                store = _CoderFile(path)
                if store._data.get("id") and store._data.get("name"):
                    self._files[store._data["id"]] = store

    def list(self) -> list[dict]:
        return sorted((dict(f._data) for f in self._files.values()), key=lambda c: c["name"].lower())

    def get(self, coder_id: str) -> dict | None:
        store = self._files.get(coder_id)
        return dict(store._data) if store else None

    def find_by_name(self, name: str) -> dict | None:
        wanted = _clean(name).lower()
        return next((c for c in self.list() if c["name"].lower() == wanted), None)

    def _check_initials(self, initials, exclude: str | None = None) -> str:
        initials = _clean(initials).replace(" ", "")
        if not initials:
            raise CoderError("a coder needs initials")
        for coder in self.list():
            if coder["id"] != exclude and coder["initials"].lower() == initials.lower():
                raise CoderError(f"{coder['name']} already uses the initials {coder['initials']}")
        return initials

    def create(self, name, initials) -> dict:
        """Add a coder, or continue as the one who already has this name."""
        name = _clean(name)
        if not name:
            raise CoderError("a coder needs a name")
        existing = self.find_by_name(name)
        if existing:
            return existing
        initials = self._check_initials(initials)
        coder_id = uuid.uuid4().hex[:12]
        store = _CoderFile(self.folder / coder_id / CODER_FILENAME)
        store._data = {"id": coder_id, "name": name, "initials": initials, "created_at": now()}
        store._write()
        self._files[coder_id] = store
        return dict(store._data)

    def update(self, coder_id: str, patch: dict) -> dict:
        store = self._files.get(coder_id)
        if store is None:
            raise KeyError(coder_id)
        if "name" in patch:
            name = _clean(patch["name"])
            if not name:
                raise CoderError("a coder needs a name")
            other = self.find_by_name(name)
            if other and other["id"] != coder_id:
                raise CoderError(f"there is already a coder called {other['name']}")
            store._data["name"] = name
        if "initials" in patch:
            store._data["initials"] = self._check_initials(patch["initials"], exclude=coder_id)
        store._write()
        return dict(store._data)

    def suggest(self, name: str, exclude: str | None = None) -> str:
        taken = [c["initials"] for c in self.list() if c["id"] != exclude]
        return suggest_initials(name, taken)
