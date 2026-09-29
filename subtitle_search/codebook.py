"""A coder's list of codes, for text or for video.

Text codes (what was said, applied to quotes) and video codes (what was done,
applied to spans of the recording) are separate codebooks that are never merged.
They behave the same way, though, so they share this class: a code has an id, a
unique name, a colour and a description, and quotes or spans refer to it by id so
a rename is one edit that every use follows. Video codes can also carry a
one-key shortcut; text codes cannot.
"""

from __future__ import annotations

import uuid

from .jsonstore import JsonStore, now

SCHEMA_VERSION = 1

#: Named here and drawn by the frontend, so the stored file never hard-codes a hex
#: value that a restyle would orphan.
COLORS = ("blue", "orange", "green", "magenta", "slate", "gold", "cyan", "brick")


class CodebookError(ValueError):
    """A request that cannot be applied, reported back as a 400."""


def _clean_name(name) -> str:
    return " ".join(str(name or "").split())


class Codebook(JsonStore):
    """One coder's codes of one kind."""

    #: Keys the reader already uses, for codebooks whose codes carry shortcuts.
    #: None means codes of this kind have no key at all.
    reserved_keys: frozenset | None = None

    def _empty(self) -> dict:
        return {"version": SCHEMA_VERSION, "updated_at": now(), "codes": []}

    def _repair(self, data: dict) -> dict:
        data.setdefault("version", SCHEMA_VERSION)
        if not isinstance(data.get("codes"), list):
            data["codes"] = []
        return data

    def list(self) -> list[dict]:
        return sorted(self._data["codes"], key=lambda c: str(c.get("name", "")).lower())

    def get(self, code_id: str) -> dict | None:
        return next((c for c in self._data["codes"] if c.get("id") == code_id), None)

    def find(self, name) -> dict | None:
        wanted = _clean_name(name).lower()
        return next((c for c in self._data["codes"] if str(c.get("name", "")).lower() == wanted), None)

    def _check_name(self, name, exclude: str | None = None) -> str:
        name = _clean_name(name)
        if not name:
            raise CodebookError("a code needs a name")
        for code in self._data["codes"]:
            if code.get("id") != exclude and str(code.get("name", "")).lower() == name.lower():
                raise CodebookError(f"there is already a code called '{code['name']}'")
        return name

    def _check_key(self, raw, exclude: str | None = None) -> str | None:
        key = str(raw or "").strip()
        if not key:
            return None
        if len(key) != 1:
            raise CodebookError("a code's key must be a single character")
        if key.lower() in self.reserved_keys:
            raise CodebookError(f"'{key}' is already used by the reader")
        for code in self._data["codes"]:
            if code.get("id") != exclude and code.get("key") == key:
                raise CodebookError(f"'{key}' is already the key for '{code['name']}'")
        return key

    def _next_color(self) -> str:
        used = [c.get("color") for c in self._data["codes"]]
        return min(COLORS, key=lambda color: (used.count(color), COLORS.index(color)))

    def add(self, payload: dict) -> dict:
        color = payload.get("color")
        code = {
            "id": uuid.uuid4().hex[:12],
            "name": self._check_name(payload.get("name")),
            "color": color if color in COLORS else self._next_color(),
            "description": str(payload.get("description") or ""),
            "created_at": now(),
        }
        if self.reserved_keys is not None:
            code["key"] = self._check_key(payload.get("key"))
        self._data["codes"].append(code)
        self._write()
        return code

    def update(self, code_id: str, patch: dict) -> dict:
        code = self.get(code_id)
        if code is None:
            raise KeyError(code_id)
        if "name" in patch:
            code["name"] = self._check_name(patch["name"], exclude=code_id)
        if "color" in patch:
            if patch["color"] not in COLORS:
                raise CodebookError("unknown color")
            code["color"] = patch["color"]
        if "description" in patch:
            code["description"] = str(patch["description"] or "")
        if "key" in patch and self.reserved_keys is not None:
            code["key"] = self._check_key(patch["key"], exclude=code_id)
        self._write()
        return code

    def remove(self, code_id: str) -> bool:
        code = self.get(code_id)
        if code is None:
            return False
        self._data["codes"].remove(code)
        self._write()
        return True


class TextCodebook(Codebook):
    """Codes applied to quotes. Several can sit on one quote."""
