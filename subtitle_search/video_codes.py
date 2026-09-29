"""Video codes: named spans of time in the recording.

Video codes mark what someone *did* (``scroll`` from 1:02 to 1:30); text codes
mark what was said. The two codebooks are separate and never merged.

- ``VideoCodebook``: one coder's codes, ``<study>/coders/<id>/video_codebook.json``.
  Spans refer to a code by id, so a rename follows everywhere.
- ``VideoCodeStore``: one coder's spans in one recording,
  ``<recording>/coders/<id>/video_codes.json``, in session seconds, so a span can
  cross an interruption and never moves when a caption is corrected.
- ``SessionVideoCodes``: every coder's spans plus common spans for a recording;
  it writes only to the caller's own store.
"""

from __future__ import annotations

import uuid

from .codebook import COLORS, Codebook, CodebookError
from .jsonstore import JsonStore, now

SCHEMA_VERSION = 1

#: Per coder: the codebook at ``<study>/coders/<id>/``, the spans at
#: ``<recording>/coders/<id>/``.
CODEBOOK_FILENAME = "video_codebook.json"
SPANS_FILENAME = "video_codes.json"

#: The shared files from before coders existed. Read by nothing; only reported.
LEGACY_CODEBOOK_FILENAME = "library.video_codebook.json"
LEGACY_SPANS_FILENAME = "session.video_codes.json"

#: Keys the reader and the coding view already use. A code hotkey that shadowed one would silently
#: stop that key working. Speaker keys are checked in the browser, since the
#: roster lives in each transcript rather than here.
RESERVED_KEYS = frozenset("jkhcefs/[]iox,.m 0123456789")


#: Raised for both codebook and span errors; one error type keeps the routes simple.
VideoCodeError = CodebookError


class VideoCodebook(Codebook):
    """One coder's video codes. Unlike text codes they can carry a shortcut key."""

    reserved_keys = RESERVED_KEYS


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


class SessionVideoCodes:
    """Every coder's spans in one recording, one store per coder.

    Like the quotes: reads are everyone's, labelled with the coder; writes go to
    the coder's own store and never touch anyone else's spans.
    """

    def __init__(self, stores: dict[str, VideoCodeStore], factory=None, common=None):
        self.stores = dict(stores)
        self.factory = factory
        #: The agreed spans of this recording (a common.CommonSet), shown to everyone.
        self.common = common

    @staticmethod
    def _label(coder: str, span: dict) -> dict:
        return {**span, "coder": coder}

    def list(self) -> list[dict]:
        spans = [self._label(c, s) for c, store in self.stores.items() for s in store.list()]
        if self.common is not None:
            spans += self.common.list()
        return sorted(spans, key=lambda s: (s.get("start", 0.0), s.get("end", 0.0)))

    def count(self, code_id: str) -> int:
        return sum(store.count(code_id) for store in self.stores.values())

    def reassign(self, from_id: str, to_id: str) -> int:
        return sum(store.reassign(from_id, to_id) for store in self.stores.values())

    def _own(self, coder: str, span_id: str) -> VideoCodeStore:
        owner = next((c for c, s in self.stores.items() if s._find(span_id)), None)
        if owner is None:
            raise KeyError(span_id)
        if owner != coder:
            raise PermissionError("that span belongs to another coder")
        return self.stores[owner]

    def store_for(self, coder: str) -> VideoCodeStore:
        if coder not in self.stores:
            if self.factory is None:
                raise KeyError(coder)
            self.stores[coder] = self.factory(coder)
        return self.stores[coder]

    def add(self, coder: str, payload: dict, codebook: VideoCodebook, duration: float | None) -> dict:
        return self._label(coder, self.store_for(coder).add(payload, codebook, duration))

    def update(self, coder: str, span_id: str, patch: dict, codebook: VideoCodebook, duration: float | None) -> dict:
        return self._label(coder, self._own(coder, span_id).update(span_id, patch, codebook, duration))

    def remove(self, coder: str, span_id: str) -> bool:
        return self._own(coder, span_id).remove(span_id)


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
