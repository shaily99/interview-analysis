"""Video codes: named spans of time in the recording.

Quotes are anchored to words and labelled with tags. Video codes are the other
half of coding an interview -- what someone *did* rather than what they said:
``scroll`` from 1:02 to 1:30, ``hesitates`` over a button. They are kept wholly
separate from tags, on purpose. A tag and a video code can share a name without
having anything to do with each other, and neither vocabulary ever suggests the
other's entries.

Two files hold them.

The codebook, ``library.video_codebook.json`` at the library root, is the list of
codes, shared by every recording so a study codes consistently. Each code has an
id, and spans refer to that id rather than the name, so a rename or a recolor is
one edit that every span follows.

The spans, ``session.video_codes.json`` in each recording folder, are the coded
stretches of that session. Times are session seconds -- the same continuous
timeline as cues and quotes -- so a span can cross the seam of an interrupted
recording. Spans do not depend on the transcript at all, so correcting a caption
can never move one.
"""

from __future__ import annotations

import uuid

from .jsonstore import JsonStore, now

SCHEMA_VERSION = 1

CODEBOOK_FILENAME = "library.video_codebook.json"
SPANS_FILENAME = "session.video_codes.json"

#: Named here and drawn by the frontend, like quote colors, but a separate set:
#: a video code should never be mistaken for a highlight.
COLORS = ("blue", "orange", "green", "magenta", "slate", "gold", "cyan", "brick")

#: Keys the reader already uses. A code hotkey that shadowed one would silently
#: stop that key working. Speaker keys are checked in the browser, since the
#: roster lives in each transcript rather than here.
RESERVED_KEYS = frozenset("jkhcefs/[]iox 0123456789")


class VideoCodeError(ValueError):
    """A request that cannot be applied, reported back as a 400."""


def _clean_key(raw) -> str | None:
    key = str(raw or "").strip()
    if not key:
        return None
    if len(key) != 1:
        raise VideoCodeError("a code's key must be a single character")
    if key.lower() in RESERVED_KEYS:
        raise VideoCodeError(f"'{key}' is already used by the reader")
    return key


class VideoCodebook(JsonStore):
    """The library's list of video codes."""

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

    def _check_name(self, name: str, exclude: str | None = None) -> str:
        name = " ".join(str(name or "").split())
        if not name:
            raise VideoCodeError("a code needs a name")
        for code in self._data["codes"]:
            if code.get("id") != exclude and str(code.get("name", "")).lower() == name.lower():
                raise VideoCodeError(f"there is already a code called '{code['name']}'")
        return name

    def _check_key(self, raw, exclude: str | None = None) -> str | None:
        key = _clean_key(raw)
        if key:
            for code in self._data["codes"]:
                if code.get("id") != exclude and code.get("key") == key:
                    raise VideoCodeError(f"'{key}' is already the key for '{code['name']}'")
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
            "key": self._check_key(payload.get("key")),
            "created_at": now(),
        }
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
                raise VideoCodeError("unknown color")
            code["color"] = patch["color"]
        if "description" in patch:
            code["description"] = str(patch["description"] or "")
        if "key" in patch:
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


class VideoCodeStore(JsonStore):
    """One recording's coded spans."""

    def _empty(self) -> dict:
        return {"version": SCHEMA_VERSION, "updated_at": now(), "spans": []}

    def _repair(self, data: dict) -> dict:
        data.setdefault("version", SCHEMA_VERSION)
        if not isinstance(data.get("spans"), list):
            data["spans"] = []
        return data

    def list(self) -> list[dict]:
        return sorted(self._data["spans"], key=lambda s: (s.get("start", 0.0), s.get("end", 0.0)))

    def count(self, code_id: str) -> int:
        return sum(1 for s in self._data["spans"] if s.get("code_id") == code_id)

    def _find(self, span_id: str) -> dict | None:
        return next((s for s in self._data["spans"] if s.get("id") == span_id), None)

    @staticmethod
    def _times(start, end, duration: float | None) -> tuple[float, float]:
        try:
            start, end = round(float(start), 3), round(float(end), 3)
        except (TypeError, ValueError) as exc:
            raise VideoCodeError("start and end must be numbers of seconds") from exc
        if start < 0:
            raise VideoCodeError("a span cannot start before the recording")
        if end <= start:
            raise VideoCodeError("a span has to end after it starts")
        if duration and end > duration + 0.5:
            raise VideoCodeError("a span cannot run past the end of the recording")
        return start, min(end, duration) if duration else end

    def add(self, payload: dict, codebook: VideoCodebook, duration: float | None) -> dict:
        code_id = str(payload.get("code_id") or "")
        if codebook.get(code_id) is None:
            raise VideoCodeError("unknown video code")
        start, end = self._times(payload.get("start"), payload.get("end"), duration)
        timestamp = now()
        span = {
            "id": uuid.uuid4().hex[:12],
            "code_id": code_id,
            "start": start,
            "end": end,
            "note": str(payload.get("note") or ""),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        self._data["spans"].append(span)
        self._write()
        return span

    def update(
        self, span_id: str, patch: dict, codebook: VideoCodebook, duration: float | None
    ) -> dict:
        span = self._find(span_id)
        if span is None:
            raise KeyError(span_id)
        # Validate everything before touching the record, so a bad patch is a
        # no-op rather than half applied.
        if "code_id" in patch and codebook.get(str(patch["code_id"])) is None:
            raise VideoCodeError("unknown video code")
        start, end = self._times(
            patch.get("start", span.get("start")), patch.get("end", span.get("end")), duration
        )
        if "code_id" in patch:
            span["code_id"] = str(patch["code_id"])
        span["start"], span["end"] = start, end
        if "note" in patch:
            span["note"] = str(patch["note"] or "")
        span["updated_at"] = now()
        self._write()
        return span

    def remove(self, span_id: str) -> bool:
        span = self._find(span_id)
        if span is None:
            return False
        self._data["spans"].remove(span)
        self._write()
        return True

    def reassign(self, from_id: str, to_id: str) -> int:
        moved = 0
        for span in self._data["spans"]:
            if span.get("code_id") == from_id:
                span["code_id"] = to_id
                span["updated_at"] = now()
                moved += 1
        if moved:
            self._write()
        return moved


# -- across the library -------------------------------------------------------


def usage(registry, code_id: str) -> int:
    """How many spans, in every recording, carry this code."""
    return sum(r.video_codes.count(code_id) for r in registry.list())


def usage_counts(registry) -> dict[str, int]:
    counts: dict[str, int] = {}
    for recording in registry.list():
        for span in recording.video_codes.list():
            counts[span["code_id"]] = counts.get(span["code_id"], 0) + 1
    return counts


def delete_code(registry, codebook: VideoCodebook, code_id: str) -> None:
    """Remove a code nothing uses. One still in use has to be merged instead."""
    if codebook.get(code_id) is None:
        raise KeyError(code_id)
    used = usage(registry, code_id)
    if used:
        raise VideoCodeError(
            f"{used} span{'s' if used != 1 else ''} still use this code -- merge it into another"
        )
    codebook.remove(code_id)


def merge_code(registry, codebook: VideoCodebook, code_id: str, into: str) -> int:
    """Move every span of one code onto another, then drop the first."""
    if codebook.get(code_id) is None:
        raise KeyError(code_id)
    if codebook.get(into) is None:
        raise VideoCodeError("unknown video code to merge into")
    if into == code_id:
        raise VideoCodeError("a code cannot be merged into itself")
    moved = sum(r.video_codes.reassign(code_id, into) for r in registry.list())
    codebook.remove(code_id)
    return moved
