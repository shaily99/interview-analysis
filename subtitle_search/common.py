"""Common codes: what the coders have agreed, and where it is kept.

Codes start out in one coder's independent set. Once they have been discussed and
agreed they are moved to common, where every coder sees them in every mode and
anyone may edit them. A study is shared through a synced folder, so common
records have to survive several people changing them on different machines.

Each common record -- a code, a quote, a span, an item returned to a coder --
exists in several copies: one in the shared ``common/`` folder, and one in each
coder's own ``coders/<id>/common/`` folder. A change is only ever written to the
changing coder's own copy, stamped with when and by whom; what everyone sees is
the newest version of each record across all the copies. A deletion keeps the
record, marked deleted, so it still wins over older copies. Refresh writes the
combined result into the shared file, which is then a compact summary that could
always be rebuilt from the coders' copies.

That is what makes the shared folder safe for this: the only files that are
ever a source of change each have one writer, so a sync conflict on the shared
file loses nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .coders import CODERS_DIRNAME
from .jsonstore import JsonStore

COMMON_DIRNAME = "common"
SCHEMA_VERSION = 1

#: The coder label common items carry, wherever an item names its coder.
COMMON = "common"


def stamp() -> str:
    """A change time fine enough that two quick edits never tie."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _later_than(previous: dict | None) -> str:
    """A stamp later than the version it replaces, whatever this machine's clock says.

    Newest wins, so a change made on a computer whose clock is behind would
    otherwise lose to the older version it was meant to replace.
    """
    now = stamp()
    if previous and previous.get("updated_at", "") >= now:
        try:
            bumped = datetime.fromisoformat(previous["updated_at"]) + timedelta(microseconds=1)
            return bumped.isoformat(timespec="microseconds")
        except ValueError:
            pass
    return now


def _newer(a: dict | None, b: dict) -> bool:
    """Whether ``b`` should replace ``a``: later change, ties broken by coder id."""
    if a is None:
        return True
    return (b.get("updated_at", ""), b.get("updated_by", "")) > (a.get("updated_at", ""), a.get("updated_by", ""))


class _RecordFile(JsonStore):
    def _empty(self) -> dict:
        return {"version": SCHEMA_VERSION, "records": {}}

    def _repair(self, data: dict) -> dict:
        if not isinstance(data.get("records"), dict):
            data["records"] = {}
        return data


class CommonSet:
    """One kind of common record under one folder, across its shared and coder copies.

    ``base`` is the study root (for codebooks) or a recording folder (for its
    quotes, spans and returned items); ``name`` is the file name in each copy.
    """

    def __init__(self, base: Path, name: str):
        self.base = Path(base)
        self.name = name
        self.reload()

    @property
    def shared_path(self) -> Path:
        return self.base / COMMON_DIRNAME / self.name

    def own_path(self, coder: str) -> Path:
        return self.base / CODERS_DIRNAME / coder / COMMON_DIRNAME / self.name

    def _coders(self) -> list[str]:
        folder = self.base / CODERS_DIRNAME
        return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []

    def reload(self) -> None:
        """Read every copy again and combine them."""
        self._shared = _RecordFile(self.shared_path)
        self._own = {c: _RecordFile(self.own_path(c)) for c in self._coders() if self.own_path(c).is_file()}
        merged: dict[str, dict] = {}
        for copy in [self._shared, *self._own.values()]:
            if copy.unreadable:
                continue  # someone's copy mid-sync: skipped, never touched
            for record_id, record in copy._data["records"].items():
                if _newer(merged.get(record_id), record):
                    merged[record_id] = record
        self._merged = merged
        #: True when some copy could not be read, so what is seen may be missing records.
        self.incomplete = any(copy.unreadable for copy in [self._shared, *self._own.values()])

    # -- reading ------------------------------------------------------------

    def get(self, record_id: str) -> dict | None:
        record = self._merged.get(record_id)
        return None if record is None or record.get("deleted") else record

    def list(self) -> list[dict]:
        return [r for r in self._merged.values() if not r.get("deleted")]

    def conflict_copies(self) -> list[str]:
        """Copies of the shared file a sync client made when two writes crossed."""
        folder = self.shared_path.parent
        if not folder.is_dir():
            return []
        stem, suffix = self.shared_path.stem, self.shared_path.suffix
        return sorted(
            str(p.relative_to(self.base))
            for p in folder.iterdir()
            if p.name != self.shared_path.name and p.name.startswith(stem) and p.name.endswith(suffix)
        )

    # -- writing: always to the writer's own copy ----------------------------

    def _own_copy(self, coder: str) -> _RecordFile:
        if coder not in self._own:
            self._own[coder] = _RecordFile(self.own_path(coder))
        return self._own[coder]

    def put(self, coder: str, record: dict) -> dict:
        record = {**record, "updated_at": _later_than(self._merged.get(record["id"])), "updated_by": coder}
        record.pop("deleted", None)
        copy = self._own_copy(coder)
        copy._data["records"][record["id"]] = record
        copy._write()
        self._merged[record["id"]] = record
        return record

    def delete(self, coder: str, record_id: str) -> bool:
        current = self.get(record_id)
        if current is None:
            return False
        tombstone = {"id": record_id, "deleted": True, "updated_at": _later_than(self._merged.get(record_id)), "updated_by": coder}
        copy = self._own_copy(coder)
        copy._data["records"][record_id] = tombstone
        copy._write()
        self._merged[record_id] = tombstone
        return True

    def push(self) -> None:
        """Write the combined records, deletions included, into the shared file."""
        self._shared._data["records"] = dict(self._merged)
        self._shared._write()


class CommonCodebook(CommonSet):
    """The agreed codes of one kind, which anyone may edit.

    Behaves like a coder's codebook -- unique names, a colour, a description --
    except that every change is written to the changing coder's own copy.
    Common codes carry no shortcut key; each coder codes with their own
    same-named code and its key.
    """

    def list(self) -> list[dict]:
        return sorted(super().list(), key=lambda c: str(c.get("name", "")).lower())

    def find(self, name) -> dict | None:
        wanted = " ".join(str(name or "").split()).lower()
        return next((c for c in self.list() if str(c.get("name", "")).lower() == wanted), None)

    def _check_name(self, name, exclude: str | None = None) -> str:
        from .codebook import CodebookError

        name = " ".join(str(name or "").split())
        if not name:
            raise CodebookError("a code needs a name")
        clash = self.find(name)
        if clash and clash["id"] != exclude:
            raise CodebookError(f"there is already a common code called '{clash['name']}'")
        return name

    def add(self, payload: dict, coder: str) -> dict:
        import uuid

        from .codebook import COLORS

        color = payload.get("color")
        code = {
            "id": uuid.uuid4().hex[:12],
            "name": self._check_name(payload.get("name")),
            "color": color if color in COLORS else COLORS[0],
            "description": str(payload.get("description") or ""),
        }
        return self.put(coder, code)

    def update(self, code_id: str, patch: dict, coder: str) -> dict:
        from .codebook import COLORS, CodebookError

        code = self.get(code_id)
        if code is None:
            raise KeyError(code_id)
        code = dict(code)
        if "name" in patch:
            code["name"] = self._check_name(patch["name"], exclude=code_id)
        if "color" in patch:
            if patch["color"] not in COLORS:
                raise CodebookError("unknown color")
            code["color"] = patch["color"]
        if "description" in patch:
            code["description"] = str(patch["description"] or "")
        return self.put(coder, code)

    def remove(self, code_id: str, coder: str) -> bool:
        return self.delete(coder, code_id)


# -- common quotes and spans: where they are, and who put each code on them ----------
#
# A common quote is one record saying where it is (its anchors, times and text),
# and one record per coder per code saying "I put this code here". The two are
# kept apart so that two coders acting at once never overwrite each other: each
# writes only their own contribution records, and a caption correction rewrites
# only where a quote is. A quote's record is never deleted; a quote shows while
# any contribution to it is live. A span is the same: one record for the code
# and its times, and one per contributing coder.

import copy as _copy  # noqa: E402
import hashlib as _hashlib  # noqa: E402
import uuid as _uuid  # noqa: E402

from .codebook import CodebookError  # noqa: E402

#: Two coders' spans of the same code count as one when both ends are this close.
SPAN_TOLERANCE = 0.5

ANCHORS = ("start_cue_id", "start_char_offset", "end_cue_id", "end_char_offset")
QUOTE_FIELDS = (*ANCHORS, "start_time", "end_time", "text", "speaker", "color")


def _anchors(item: dict) -> tuple:
    return tuple(item.get(k) for k in ANCHORS)


def _live(contributions: CommonSet, key: str, value) -> list[dict]:
    return [c for c in contributions.list() if c.get(key) == value]


class CommonQuotes:
    """One recording's common quotes: where each is, and who coded it with what."""

    def __init__(self, folder: Path, quotes_name: str, codes_name: str):
        self.places = CommonSet(folder, quotes_name)
        self.codes = CommonSet(folder, codes_name)

    def sets(self):
        return (self.places, self.codes)

    def reload(self) -> None:
        self.places.reload()
        self.codes.reload()

    @property
    def incomplete(self) -> bool:
        return self.places.incomplete or self.codes.incomplete

    def _view(self, place: dict, contributions: list[dict]) -> dict:
        pairs: dict[str, list[dict]] = {}
        for c in contributions:
            pairs.setdefault(c["code_id"], []).append(c)
        notes, seen = [], set()
        for c in contributions:
            if c.get("note") and (c["coder"], c["note"]) not in seen:
                seen.add((c["coder"], c["note"]))
                notes.append({"coder": c["coder"], "note": c["note"]})
        return {
            **{k: v for k, v in place.items() if k not in ("updated_at", "updated_by")},
            "coder": COMMON,
            "codes": list(pairs),
            "pairs": [{"code_id": k, "contributors": v} for k, v in pairs.items()],
            "note": "",
            "notes": notes,
            "contributors": sorted({c["coder"] for c in contributions}),
        }

    def list(self) -> list[dict]:
        by_quote: dict[str, list[dict]] = {}
        for c in self.codes.list():
            by_quote.setdefault(c["quote"], []).append(c)
        return [self._view(p, by_quote[p["id"]]) for p in self.places.list() if p["id"] in by_quote]

    def get(self, quote_id: str) -> dict | None:
        place = self.places.get(quote_id)
        contributions = _live(self.codes, "quote", quote_id)
        return self._view(place, contributions) if place and contributions else None

    def find(self, source: dict) -> dict | None:
        """The common quote on exactly these words, if there is one."""
        return next((p for p in self.places.list() if _anchors(p) == _anchors(source)), None)

    def add(self, coder: str, source: dict, code_id: str) -> bool:
        """Record that ``coder`` put ``code_id`` on the words of ``source`` (their own quote).

        Returns whether another coder had already put that code on the same words,
        which makes this an exact duplicate, combined into one application.
        """
        place = self.find(source)
        if place is None:
            # Named after the words, so two coders creating it at once create the same one.
            key = "|".join(str(v) for v in _anchors(source))
            quote_id = _hashlib.sha1(key.encode()).hexdigest()[:12]
            if self.places.get(quote_id) is None:
                self.places.put(coder, {"id": quote_id, **{k: source.get(k) for k in QUOTE_FIELDS}})
        else:
            quote_id = place["id"]
        others = [c for c in _live(self.codes, "quote", quote_id) if c["code_id"] == code_id]
        part_id = f"{quote_id}:{code_id}:{coder}:{source['id']}"
        if self.codes.get(part_id) is None:
            self.codes.put(coder, {"id": part_id, "quote": quote_id, "code_id": code_id, "coder": coder,
                                   "quote_id": source["id"], "note": source.get("note", "")})
        return any(c["id"] != part_id for c in others)

    def remove_code(self, actor: str, quote_id: str, code_id: str) -> list[dict]:
        """Take a code off a common quote; returns the contributions taken off."""
        removed = [c for c in _live(self.codes, "quote", quote_id) if c["code_id"] == code_id]
        for c in removed:
            self.codes.delete(actor, c["id"])
        return removed

    def remap(self, actor: str, method: str, transcript, *args) -> list[dict]:
        """Re-anchor where common quotes are after a caption correction, as ``actor``."""
        from .highlights import HighlightStore, _Scratch

        scratch = _Scratch([dict(p) for p in self.places.list()], transcript)
        moved = getattr(HighlightStore, method)(scratch, *args)
        for place in moved:
            self.places.put(actor, {k: v for k, v in place.items() if k not in ("updated_at", "updated_by")})
        return [v for v in (self.get(p["id"]) for p in moved) if v]


class CommonSpans:
    """One recording's common spans: each code and its times, and who coded it."""

    def __init__(self, folder: Path, spans_name: str, parts_name: str):
        self.spans = CommonSet(folder, spans_name)
        self.parts = CommonSet(folder, parts_name)

    def sets(self):
        return (self.spans, self.parts)

    def reload(self) -> None:
        self.spans.reload()
        self.parts.reload()

    @property
    def incomplete(self) -> bool:
        return self.spans.incomplete or self.parts.incomplete

    def _view(self, span: dict, parts: list[dict]) -> dict:
        return {
            **{k: v for k, v in span.items() if k not in ("updated_at", "updated_by")},
            "coder": COMMON,
            "note": "",
            "notes": [{"coder": p["coder"], "note": p["note"]} for p in parts if p.get("note")],
            "contributors": sorted({p["coder"] for p in parts}),
            "parts": parts,
        }

    def list(self) -> list[dict]:
        by_span: dict[str, list[dict]] = {}
        for p in self.parts.list():
            by_span.setdefault(p["span"], []).append(p)
        return [self._view(s, by_span[s["id"]]) for s in self.spans.list() if s["id"] in by_span]

    def get(self, span_id: str) -> dict | None:
        span = self.spans.get(span_id)
        parts = _live(self.parts, "span", span_id)
        return self._view(span, parts) if span and parts else None

    def add(self, coder: str, source: dict, code_id: str) -> bool:
        """Record ``coder``'s span as an application of common ``code_id``.

        A live common span of that code whose ends are each within half a second
        is the same application: the coder is added to it, and it counts as an
        exact duplicate.
        """
        match = next((s for s in self.list() if s["code_id"] == code_id
                      and abs(s["start"] - source["start"]) <= SPAN_TOLERANCE
                      and abs(s["end"] - source["end"]) <= SPAN_TOLERANCE), None)
        if match is None:
            span_id = _uuid.uuid4().hex[:12]
            self.spans.put(coder, {"id": span_id, "code_id": code_id, "start": source["start"], "end": source["end"]})
        else:
            span_id = match["id"]
        part_id = f"{span_id}:{coder}:{source['id']}"
        if self.parts.get(part_id) is None:
            self.parts.put(coder, {"id": part_id, "span": span_id, "coder": coder, "span_id": source["id"],
                                   "start": source["start"], "end": source["end"], "note": source.get("note", "")})
        return match is not None and any(p["id"] != part_id for p in match["parts"])

    def remove(self, actor: str, span_id: str) -> list[dict]:
        removed = _live(self.parts, "span", span_id)
        for p in removed:
            self.parts.delete(actor, p["id"])
        return removed


# -- moving codes to common, and giving them back ---------------------------------

HISTORY_FILENAME = "history.json"


class ClashError(CodebookError):
    """A code of that name is already common; the coder chooses to merge or rename."""

    def __init__(self, code: dict):
        super().__init__(f"there is already a common code called '{code['name']}'")
        self.code = code


class _History(JsonStore):
    def _empty(self) -> dict:
        return {"version": SCHEMA_VERSION, "entries": []}

    def _repair(self, data: dict) -> dict:
        if not isinstance(data.get("entries"), list):
            data["entries"] = []
        return data


def _record_history(registry, coder: str, entry: dict) -> None:
    """Add to the acting coder's own history file, the only one they write."""
    log = _History(registry.root / CODERS_DIRNAME / coder / HISTORY_FILENAME)
    log._data["entries"].append({"at": stamp(), "coder": coder, **entry})
    log._write()


def history_for(registry, kind: str, code_id: str) -> list[dict]:
    """Every coder's history of one common code, newest first."""
    entries = []
    folder = registry.root / CODERS_DIRNAME
    for path in sorted(folder.glob(f"*/{HISTORY_FILENAME}")) if folder.is_dir() else []:
        log = _History(path)
        if not log.unreadable:
            entries += [e for e in log._data["entries"] if e.get("kind") == kind and e.get("code_id") == code_id]
    return sorted(entries, key=lambda e: e["at"], reverse=True)


def _books(registry, kind: str):
    own = registry.books.text if kind == "text" else registry.books.video
    common = registry.books.common_text if kind == "text" else registry.books.common_video
    return own, common


def _require_complete(registry, kind: str, coder: str | None = None) -> None:
    """Refuse to move or return while part of the study cannot be read.

    Otherwise the files that could not be read keep pointing at a code that no
    longer exists, and their quotes or spans are neither moved nor given back.
    """
    if registry.failures:
        names = ", ".join(name for name, _ in registry.failures)
        raise CodebookError(f"some recordings could not be read ({names}); wait for the folder to finish syncing and Refresh first")
    for recording in registry.list():
        stores = (recording.store if kind == "text" else recording.video_codes).stores
        common = recording.common_quotes if kind == "text" else recording.common_spans
        own = stores.get(coder) if coder else None
        if common.incomplete or (own is not None and own.unreadable):
            raise CodebookError(f"a file in {recording.title} could not be read; wait for it to finish syncing and Refresh first")


def move_code(registry, kind: str, coder: str, code_id: str, *, final: bool, description=None,
              name=None, into: str | None = None, dry_run: bool = False) -> dict:
    """Move one of a coder's codes, and everything carrying it, into common.

    Common records are written before the coder's own files are cleaned up, and
    a contribution already recorded is not recorded twice, so a move that was
    interrupted is finished by simply moving again.
    """
    own_book, common = _books(registry, kind)
    code = own_book(coder).get(code_id)
    if code is None:
        raise KeyError(code_id)
    if not final:
        raise CodebookError("confirm the code has been discussed and is final before moving it to common")
    _require_complete(registry, kind, coder)

    target = None
    if into:
        target = common.get(into)
        if target is None:
            raise CodebookError("that common code no longer exists")
    else:
        clash = common.find(name or code["name"])
        if clash:
            raise ClashError(clash)

    summary = {"moved": 0, "duplicates": 0, "quotes_deleted": 0, "recordings": 0}
    plans = []
    for recording in registry.list():
        store = (recording.store if kind == "text" else recording.video_codes).stores.get(coder)
        items = [i for i in store.list() if (code_id in (i.get("codes") or []) if kind == "text" else i.get("code_id") == code_id)] if store else []
        if items:
            plans.append((recording, store, items))
            summary["recordings"] += 1
    if dry_run:
        for recording, _, items in plans:
            # What the real move has combined into by each item: the target's applications, then the coder's earlier items.
            if kind == "text":
                seen = {_anchors(q) for q in recording.common_quotes.list() if target and target["id"] in q["codes"]}
            else:
                seen = [(s["start"], s["end"]) for s in recording.common_spans.list() if target and s["code_id"] == target["id"]]
            for item in items:
                summary["moved"] += 1
                if kind == "text":
                    summary["quotes_deleted"] += int(len(item["codes"]) == 1)
                    duplicate = _anchors(item) in seen
                    seen.add(_anchors(item))
                else:
                    duplicate = any(abs(start - item["start"]) <= SPAN_TOLERANCE and abs(end - item["end"]) <= SPAN_TOLERANCE
                                    for start, end in seen)
                    if not duplicate:  # a combined span keeps its first ends
                        seen.append((item["start"], item["end"]))
                summary["duplicates"] += int(duplicate)
        return summary

    # The common code first, then the common applications, then the coder's own files.
    if target is None:
        target = common.add({"name": name or code["name"], "color": code["color"],
                             "description": code.get("description", "") if description is None else description}, coder=coder)
    else:
        patch = {k: v for k, v in (("name", name), ("description", description)) if v is not None}
        if patch:
            target = common.update(target["id"], patch, coder=coder)

    for recording, _, items in plans:
        applied = recording.common_quotes if kind == "text" else recording.common_spans
        for item in items:
            summary["moved"] += 1
            summary["duplicates"] += int(applied.add(coder, item, target["id"]))
    for recording, store, items in plans:
        for item in items:
            if kind == "video":
                store.remove(item["id"])
                continue
            remaining = [c for c in item["codes"] if c != code_id]
            if remaining:
                store.update(item["id"], {"codes": remaining})
            else:
                store.delete(item["id"])
                summary["quotes_deleted"] += 1

    own_book(coder).remove(code_id)
    # The coder's themes now hold the common code in its place.
    registry.theme_store(coder).replace_ref(f"{kind}:{code_id}", f"{kind}:{target['id']}")
    _record_history(registry, coder, {"action": "moved", "kind": kind, "code_id": target["id"],
                                      "name": target["name"], "from": code["name"], **summary})
    return summary


def return_code(registry, kind: str, coder: str, code_id: str) -> dict:
    """Undo agreeing a code: every contributor gets their applications back.

    Each gets them under an independent code with the common code's current
    name, description and colour. The asking coder's come back at once; the rest
    wait in the recording's returns until each of those coders refreshes, since
    only they may write their own files.
    """
    _, common = _books(registry, kind)
    code = common.get(code_id)
    if code is None:
        raise KeyError(code_id)
    _require_complete(registry, kind)
    spec = {"name": code["name"], "description": code.get("description", ""), "color": code.get("color")}
    returned = 0
    for recording in registry.list():
        if kind == "text":
            for view in recording.common_quotes.list():
                if code_id not in view["codes"]:
                    continue
                place = {k: view.get(k) for k in (*ANCHORS, "text", "speaker", "color")}
                for c in recording.common_quotes.remove_code(coder, view["id"], code_id):
                    _hand_back(registry, recording, coder, {"id": _uuid.uuid4().hex[:12], "owner": c["coder"], "kind": "text",
                                                            "code": spec, "common_id": code_id, "quote": place, "note": c.get("note", "")})
                    returned += 1
        else:
            for view in recording.common_spans.list():
                if view["code_id"] != code_id:
                    continue
                for p in recording.common_spans.remove(coder, view["id"]):
                    _hand_back(registry, recording, coder, {"id": _uuid.uuid4().hex[:12], "owner": p["coder"], "kind": "video",
                                                            "code": spec, "common_id": code_id, "span": {"start": p["start"], "end": p["end"]},
                                                            "note": p.get("note", "")})
                    returned += 1
    common.remove(code_id, coder=coder)
    # A code that is no longer common leaves the common themes.
    registry.common_themes.writer = coder
    registry.common_themes.remove_ref(f"{kind}:{code_id}")
    _record_history(registry, coder, {"action": "returned", "kind": kind, "code_id": code_id,
                                      "name": code["name"], "returned": returned})
    return {"returned": returned}


def _hand_back(registry, recording, coder: str, item: dict) -> None:
    """Place the caller's own item now; keep everyone else's, and any that fails, for later."""
    if item["owner"] == coder:
        try:
            _claim(registry, recording, coder, item)
            return
        except (CodebookError, ValueError):
            pass  # kept in returns, so it is not lost; the next Refresh tries again
    recording.returns.put(coder, item)


def _claim(registry, recording, coder: str, item: dict) -> None:
    spec = item["code"]
    if item["kind"] == "text":
        book = registry.books.text(coder)
        code = book.find(spec["name"]) or book.add(spec)
        store = recording.store
        mine = store.stores.get(coder)
        existing = next((q for q in mine.list() if _anchors(q) == _anchors(item["quote"])), None) if mine else None
        if existing:
            codes = existing.get("codes", []) + ([code["id"]] if code["id"] not in existing.get("codes", []) else [])
            patch = {"codes": codes, **({"note": item["note"]} if item["note"] and not existing.get("note") else {})}
            store.update(coder, existing["id"], patch)
        else:
            store.create(coder, {**item["quote"], "note": item["note"], "codes": [code["id"]]})
    else:
        book = registry.books.video(coder)
        code = book.find(spec["name"]) or book.add({k: v for k, v in spec.items() if k != "key"})
        recording.video_codes.add(coder, {"code_id": code["id"], **item["span"], "note": item["note"]},
                                  book, recording.transcript.duration)
    # Moving the code had pointed this coder's themes at the common code; the
    # code they get back takes its place there.
    if item.get("common_id"):
        registry.theme_store(coder).replace_ref(f"{item['kind']}:{item['common_id']}", f"{item['kind']}:{code['id']}")


def claim_returns(registry, coder: str) -> int:
    """Take in whatever other coders returned to this coder; done on this coder's Refresh.

    An item that cannot be placed -- its words no longer resolve, its span now
    runs past the recording -- is left where it is rather than stopping the rest.
    """
    claimed = 0
    for recording in registry.list():
        for item in recording.returns.list():
            if item.get("owner") != coder:
                continue
            try:
                _claim(registry, recording, coder, item)
            except (CodebookError, ValueError):
                continue
            recording.returns.delete(coder, item["id"])
            claimed += 1
    return claimed


def remap_returns(returns: CommonSet, actor: str, method: str, transcript, *args) -> None:
    """Re-anchor text items waiting to be claimed, so they land on the same words."""
    from .highlights import HighlightStore, _Scratch

    waiting = {r["id"]: r for r in returns.list() if r.get("kind") == "text"}
    if not waiting:
        return
    scratch = _Scratch([{**r["quote"], "id": rid} for rid, r in waiting.items()], transcript)
    for moved in getattr(HighlightStore, method)(scratch, *args):
        original = waiting[moved["id"]]
        quote = {k: moved.get(k) for k in (*ANCHORS, "text", "speaker", "color")}
        returns.put(actor, {**original, "quote": quote})
