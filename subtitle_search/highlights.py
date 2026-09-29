"""Persistent quote storage.

``HighlightStore`` is one coder's quotes in one recording, in
``<recording>/coders/<id>/quotes.json``. Each quote carries ``codes``: ids from
that coder's text codebook. ``SessionQuotes`` joins every coder's store and the
common quotes for a recording; it writes only to the caller's own store.

Two properties matter here. Writes are atomic, because autosaving on every edit
means a crash would otherwise be able to truncate the file mid-write and take a
session's quotes with it. And unknown fields survive a round trip, so a file
written by a later version of this tool degrades rather than being silently
stripped on the next save.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Transcript

SCHEMA_VERSION = 1

#: One coder's quotes in one recording, at ``<recording>/coders/<id>/``.
QUOTES_FILENAME = "quotes.json"

#: Colors are named here and rendered by the frontend, so the stored file stays
#: readable and does not hard-code a hex value that a restyle would orphan.
COLORS = ("amber", "teal", "rose", "violet", "sage")
DEFAULT_COLOR = "amber"

#: Seek slightly before a quote so playback lands on its first word, not inside it.
SEEK_LEAD_IN = 0.75


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HighlightError(ValueError):
    """Raised for a highlight payload that cannot be anchored to the transcript."""


def resolve_span(transcript: Transcript, payload: dict) -> dict:
    """Resolve cue anchors into media times.

    The client sends where in the text the selection landed; times are computed
    here so the estimator has exactly one implementation, and so a stored
    highlight can be re-resolved later if that estimator improves.
    """
    start_cue = transcript.cue(payload.get("start_cue_id", ""))
    end_cue = transcript.cue(payload.get("end_cue_id", ""))
    if start_cue is None or end_cue is None:
        raise HighlightError("selection does not resolve to cues in this transcript")

    if start_cue.index > end_cue.index:
        start_cue, end_cue = end_cue, start_cue
        payload = {
            **payload,
            "start_cue_id": start_cue.id,
            "end_cue_id": end_cue.id,
            "start_char_offset": payload.get("end_char_offset", 0),
            "end_char_offset": payload.get("start_char_offset", 0),
        }

    start_offset = int(payload.get("start_char_offset") or 0)
    end_offset = int(payload.get("end_char_offset") or len(end_cue.text))

    start_time = start_cue.time_at_offset(start_offset)
    end_time = end_cue.time_at_offset(end_offset)
    if end_time < start_time:
        end_time = start_time

    return {
        "start_cue_id": start_cue.id,
        "end_cue_id": end_cue.id,
        "start_char_offset": start_offset,
        "end_char_offset": end_offset,
        "start_time": round(start_time, 3),
        "end_time": round(end_time, 3),
        "speaker": payload.get("speaker") or start_cue.speaker,
    }


def _normalize_codes(raw: Any, codebook) -> list[str]:
    """Text code ids, in order and without repeats, each one from the coder's codebook."""
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for item in raw:
        code_id = str(item).strip()
        if not code_id or code_id in seen:
            continue
        if codebook is not None and codebook.get(code_id) is None:
            raise HighlightError("that text code is not in your codebook")
        seen.append(code_id)
    return seen


class HighlightStore:
    """Reads and writes one recording's highlights file."""

    def __init__(self, path: Path, transcript: Transcript, codebook=None):
        self.path = path
        self.transcript = transcript
        #: The coder's text codebook, which every code on a quote must come from.
        self.codebook = codebook
        #: True when the file exists but could not be read. It is then left alone,
        #: and only moved aside to ``.corrupt`` when its owner saves over it.
        self.unreadable = False
        self._data = self._load()
        self._seen = self._disk_stamp()

    def _disk_stamp(self):
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def sync(self) -> None:
        """Re-read the file if something else wrote it since this store last did.

        Another coder's file is synced in by the folder's sync client while this
        tool runs. Before re-anchoring their quotes after a caption correction, the
        store must start from what is on disk now, not from what it loaded, or
        writing it back would erase whatever they saved in between.
        """
        if self._disk_stamp() != self._seen:
            self.unreadable = False
            self._data = self._load()
            self._seen = self._disk_stamp()

    # -- persistence ------------------------------------------------------

    def _empty(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "vtt_file": self.transcript.source_name,
            "vtt_sha256": self.transcript.sha256,
            "updated_at": _now(),
            "highlights": [],
        }

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Never destroy an unreadable file by overwriting it with an empty
            # one. It is left where it is -- in a synced study it may be another
            # coder's, half-synced -- and moved aside only before a write.
            self.unreadable = True
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("highlights", [])
        if not isinstance(data["highlights"], list):
            data["highlights"] = []
        return data

    def _write(self) -> None:
        if self.unreadable:
            try:
                os.replace(self.path, self.path.with_suffix(self.path.suffix + ".corrupt"))
            except OSError:
                pass
            self.unreadable = False
        self._data["updated_at"] = _now()
        self._data["vtt_file"] = self.transcript.source_name
        self._data["vtt_sha256"] = self.transcript.sha256
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Temp file in the *same* directory, so os.replace is a true atomic
        # rename rather than a cross-device copy.
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
                # The coder is known from the folder; the label is for readers only.
                data = {
                    **self._data,
                    "highlights": [
                        {k: v for k, v in h.items() if k != "coder"} for h in self._data["highlights"]
                    ],
                }
                json.dump(data, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
            self._seen = self._disk_stamp()
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise

    # -- queries ----------------------------------------------------------

    @property
    def stale(self) -> bool:
        """True when the highlights were written against a different transcript."""
        stored = self._data.get("vtt_sha256")
        return bool(stored) and stored != self.transcript.sha256

    def list(self) -> list[dict]:
        return sorted(self._data["highlights"], key=lambda h: h.get("start_time", 0.0))

    def _find(self, highlight_id: str) -> dict | None:
        return next((h for h in self._data["highlights"] if h.get("id") == highlight_id), None)

    # -- mutations --------------------------------------------------------

    def create(self, payload: dict) -> dict:
        text = str(payload.get("text") or "").strip()
        if not text:
            raise HighlightError("cannot highlight an empty selection")

        span = resolve_span(self.transcript, payload)
        codes = _normalize_codes(payload.get("codes"), self.codebook)
        color = payload.get("color") or DEFAULT_COLOR
        if color not in COLORS:
            color = DEFAULT_COLOR

        timestamp = _now()
        highlight = {
            "id": uuid.uuid4().hex[:12],
            "text": text,
            "color": color,
            "note": str(payload.get("note") or ""),
            "codes": codes,
            "created_at": timestamp,
            "updated_at": timestamp,
            **span,
        }
        self._data["highlights"].append(highlight)
        self._write()
        return highlight

    def update(self, highlight_id: str, patch: dict) -> dict:
        highlight = self._find(highlight_id)
        if highlight is None:
            raise KeyError(highlight_id)

        # Checked before anything changes, so a refused code leaves the quote as it was.
        codes = _normalize_codes(patch["codes"], self.codebook) if "codes" in patch else None

        # Merge in place so fields written by another version are carried through.
        if "note" in patch:
            highlight["note"] = str(patch["note"] or "")
        if codes is not None:
            highlight["codes"] = codes
        if "color" in patch and patch["color"] in COLORS:
            highlight["color"] = patch["color"]
        if any(key in patch for key in ("start_cue_id", "end_cue_id")):
            highlight.update(resolve_span(self.transcript, {**highlight, **patch}))
        if "text" in patch and str(patch["text"]).strip():
            highlight["text"] = str(patch["text"]).strip()

        highlight["updated_at"] = _now()
        self._write()
        return highlight

    def count_code(self, code_id: str) -> int:
        return sum(1 for h in self._data["highlights"] if code_id in (h.get("codes") or []))

    def reassign_code(self, from_id: str, to_id: str) -> int:
        """Put ``to_id`` wherever ``from_id`` was, once per quote."""
        moved = 0
        for highlight in self._data["highlights"]:
            codes = highlight.get("codes") or []
            if from_id not in codes:
                continue
            merged: list[str] = []
            for code_id in codes:
                code_id = to_id if code_id == from_id else code_id
                if code_id not in merged:
                    merged.append(code_id)
            highlight["codes"] = merged
            highlight["updated_at"] = _now()
            moved += 1
        if moved:
            self._write()
        return moved

    def delete(self, highlight_id: str) -> bool:
        highlight = self._find(highlight_id)
        if highlight is None:
            return False
        self._data["highlights"].remove(highlight)
        self._write()
        return True

    def remap_cue(self, cue_id: str, old_text: str, new_text: str) -> list[dict]:
        """Re-anchor quotes after the cue they sit in was corrected.

        Corrections happen while quotes are being saved, so an edit inside a
        quoted span has to move that quote's offsets rather than leave them
        pointing at characters that shifted underneath. The quote's own text is
        re-derived too, so a saved quote reflects the corrected transcript
        instead of preserving the error.
        """
        from .editing import remap_offset  # local import: editing imports models

        touched: list[dict] = []
        for highlight in self._data["highlights"]:
            changed = False
            if highlight.get("start_cue_id") == cue_id:
                highlight["start_char_offset"] = remap_offset(
                    old_text, new_text, int(highlight.get("start_char_offset") or 0)
                )
                changed = True
            if highlight.get("end_cue_id") == cue_id:
                highlight["end_char_offset"] = remap_offset(
                    old_text, new_text, int(highlight.get("end_char_offset") or 0)
                )
                changed = True
            if not changed:
                continue

            try:
                highlight.update(resolve_span(self.transcript, highlight))
            except HighlightError:
                continue
            refreshed = self.transcript.text_between(
                highlight["start_cue_id"],
                highlight["start_char_offset"],
                highlight["end_cue_id"],
                highlight["end_char_offset"],
            )
            if refreshed:
                highlight["text"] = refreshed
            highlight["updated_at"] = _now()
            touched.append(highlight)

        if touched:
            self._write()
        return touched

    def _remap(self, move) -> list[dict]:
        """Re-anchor every quote through ``move``, then re-resolve what changed.

        ``move`` takes ``(cue_id, offset)`` and returns where that anchor now
        lives. Splitting and merging both renumber the cues after them, and both
        move anchors that were inside the captions they touched -- only the
        arithmetic differs.
        """
        touched: list[dict] = []
        for highlight in self._data["highlights"]:
            start = move(
                highlight.get("start_cue_id", ""), int(highlight.get("start_char_offset") or 0)
            )
            end = move(
                highlight.get("end_cue_id", ""), int(highlight.get("end_char_offset") or 0)
            )
            if (start[0], start[1], end[0], end[1]) == (
                highlight.get("start_cue_id"),
                highlight.get("start_char_offset"),
                highlight.get("end_cue_id"),
                highlight.get("end_char_offset"),
            ):
                continue

            highlight["start_cue_id"], highlight["start_char_offset"] = start
            highlight["end_cue_id"], highlight["end_char_offset"] = end
            try:
                highlight.update(resolve_span(self.transcript, highlight))
            except HighlightError:
                continue
            refreshed = self.transcript.text_between(
                highlight["start_cue_id"], highlight["start_char_offset"],
                highlight["end_cue_id"], highlight["end_char_offset"],
            )
            if refreshed:
                highlight["text"] = refreshed
            highlight["updated_at"] = _now()
            touched.append(highlight)

        if touched:
            self._write()
        return touched

    def remap_split(
        self, split_index: int, split_offset: int, head_len: int, tail_lead: int
    ) -> list[dict]:
        """Re-anchor quotes after one cue became two.

        Splitting inserts a cue, and cue ids are positional, so every id after
        the split shifts by one -- without this, a quote saved earlier in the
        session would silently start pointing at its neighbour. Anchors inside
        the split cue land in whichever half now contains their words.
        """
        def move(cue_id: str, offset: int) -> tuple[str, int]:
            if not cue_id.startswith("c") or not cue_id[1:].isdigit():
                return cue_id, offset
            index = int(cue_id[1:])
            if index > split_index:
                return f"c{index + 1}", offset
            if index < split_index:
                return cue_id, offset
            if offset < split_offset:
                return cue_id, min(offset, head_len)
            return f"c{split_index + 1}", max(0, offset - split_offset - tail_lead)

        return self._remap(move)

    def remap_merge(
        self, first_index: int, starts: list[int], lengths: list[int]
    ) -> list[dict]:
        """Re-anchor quotes after a run of cues became one.

        The mirror of a split: ids after the run shift *down* by however many cues
        disappeared, and an anchor inside any of the absorbed captions moves to
        where that caption's words now sit inside the joined text.
        """
        count = len(starts)

        def move(cue_id: str, offset: int) -> tuple[str, int]:
            if not cue_id.startswith("c") or not cue_id[1:].isdigit():
                return cue_id, offset
            index = int(cue_id[1:])
            if index < first_index:
                return cue_id, offset
            if index >= first_index + count:
                return f"c{index - (count - 1)}", offset
            position = index - first_index
            return f"c{first_index}", starts[position] + min(max(offset, 0), lengths[position])

        return self._remap(move)

    def restate_speaker(self, cue_ids, speaker: str | None) -> list[dict]:
        """Say who said the quotes anchored in a run of reattributed captions.

        A quote stores its speaker, because that is what gets copied out and read
        in the sidebar months later. Reattributing the captions underneath it has
        to carry through, or the quote goes on crediting the wrong person -- and a
        misattributed quote is the one error in this tool that could end up in
        something published.
        """
        wanted = set(cue_ids)
        touched: list[dict] = []
        for highlight in self._data["highlights"]:
            if highlight.get("start_cue_id") not in wanted:
                continue
            if highlight.get("speaker") == speaker:
                continue
            highlight["speaker"] = speaker
            highlight["updated_at"] = _now()
            touched.append(highlight)
        if touched:
            self._write()
        return touched

    def restamp(self) -> None:
        """Record the transcript's new digest after an edit, so it reads as current."""
        self._write()


class SessionQuotes:
    """Every coder's quotes in one recording, one store per coder.

    Reads return everyone's quotes, each labelled with its coder, so the browser
    can show one coder's or all of them. Writes go to the coder's own store, and a
    write to someone else's quote is refused: in collaborative mode other people's
    work is visible but read-only. Transcript corrections are shared, so re-anchoring
    after a correction runs over every coder's quotes.
    """

    def __init__(self, stores: dict[str, HighlightStore], factory=None, common=None, returns=None):
        self.stores = dict(stores)
        #: Makes the store for a coder who has not saved a quote here yet.
        self.factory = factory
        #: The agreed quotes of this recording (a common.CommonQuotes), shown to everyone.
        self.common = common
        #: Items returned from common, waiting to be claimed; re-anchored with the rest.
        self.returns = returns
        #: The transcript quotes are anchored in, kept here too so common quotes can
        #: be re-anchored in a recording where no coder has a quote of their own.
        self._transcript = None
        #: Who is correcting the transcript. Common quotes re-anchored by the
        #: correction are written to this coder's own copy of common.
        self.acting: str | None = None

    @staticmethod
    def _label(coder: str, highlight: dict) -> dict:
        # In place, so a caller holding a quote sees later re-anchoring, as with
        # a single store. The store leaves the label out when it writes.
        highlight["coder"] = coder
        return highlight

    def list(self) -> list[dict]:
        quotes = [self._label(c, h) for c, store in self.stores.items() for h in store.list()]
        if self.common is not None:
            quotes += self.common.list()
        return sorted(quotes, key=lambda h: h.get("start_time", 0.0))

    def count_code(self, code_id: str) -> int:
        return sum(store.count_code(code_id) for store in self.stores.values())

    def reassign_code(self, from_id: str, to_id: str) -> int:
        return sum(store.reassign_code(from_id, to_id) for store in self.stores.values())

    def owner(self, highlight_id: str) -> str | None:
        return next((c for c, s in self.stores.items() if s._find(highlight_id)), None)

    def _own(self, coder: str, highlight_id: str) -> HighlightStore:
        owner = self.owner(highlight_id)
        if owner is None:
            raise KeyError(highlight_id)
        if owner != coder:
            raise PermissionError("that quote belongs to another coder")
        return self.stores[owner]

    def store_for(self, coder: str) -> HighlightStore:
        if coder not in self.stores:
            if self.factory is None:
                raise KeyError(coder)
            self.stores[coder] = self.factory(coder)
        return self.stores[coder]

    def create(self, coder: str, payload: dict) -> dict:
        return self._label(coder, self.store_for(coder).create(payload))

    def update(self, coder: str, highlight_id: str, patch: dict) -> dict:
        return self._label(coder, self._own(coder, highlight_id).update(highlight_id, patch))

    def delete(self, coder: str, highlight_id: str) -> bool:
        return self._own(coder, highlight_id).delete(highlight_id)

    @property
    def transcript(self):
        return self._transcript

    @transcript.setter
    def transcript(self, transcript) -> None:
        self._transcript = transcript
        for store in self.stores.values():
            store.transcript = transcript

    @property
    def stale(self) -> bool:
        return any(store.stale for store in self.stores.values())

    @property
    def unreadable(self) -> bool:
        """Whether any coder's quotes here could not be read, so the list is incomplete."""
        return any(store.unreadable for store in self.stores.values())

    def _each(self, method: str, *args) -> list[dict]:
        touched = []
        for coder, store in self.stores.items():
            store.sync()
            # A file that could not be read is someone's, mid-sync: leave it be.
            if store.unreadable:
                continue
            touched.extend(self._label(coder, h) for h in getattr(store, method)(*args))
        touched.extend(self._common_each(method, *args))
        return touched

    def _common_each(self, method: str, *args) -> list[dict]:
        """Re-anchor the common quotes, and returned items waiting to be claimed, as the acting coder."""
        from .common import remap_returns

        if self.common is None or not self.acting or self.transcript is None:
            return []
        moved = self.common.remap(self.acting, method, self.transcript, *args)
        if self.returns is not None:
            remap_returns(self.returns, self.acting, method, self.transcript, *args)
        return moved

    def remap_cue(self, cue_id: str, old_text: str, new_text: str) -> list[dict]:
        return self._each("remap_cue", cue_id, old_text, new_text)

    def remap_split(self, split_index: int, split_offset: int, head_len: int, tail_lead: int) -> list[dict]:
        return self._each("remap_split", split_index, split_offset, head_len, tail_lead)

    def remap_merge(self, first_index: int, starts: list[int], lengths: list[int]) -> list[dict]:
        return self._each("remap_merge", first_index, starts, lengths)

    def restate_speaker(self, cue_ids, speaker: str | None) -> list[dict]:
        return self._each("restate_speaker", list(cue_ids), speaker)

    def restamp(self) -> None:
        for store in self.stores.values():
            store.sync()
            if not store.unreadable:
                store.restamp()


class _Scratch:
    """Common quotes held like a store's, so HighlightStore's re-anchoring can run on them.

    Nothing is written from here; the caller puts the records that moved back
    into common as the coder making the correction.
    """

    def __init__(self, highlights: list[dict], transcript):
        self._data = {"highlights": highlights}
        self.transcript = transcript

    def _write(self) -> None:
        pass

    def _remap(self, move):
        return HighlightStore._remap(self, move)
