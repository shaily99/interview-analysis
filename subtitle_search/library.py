"""Working across a whole set of recordings.

Flattens every recording's quotes (all coders' and common) into one corpus for
the library and themes pages, filtered by mode, and stores the themes.

A theme holds codes, as refs ``text:<id>`` or ``video:<id>``, never copies.

- ``ThemeStore``: one coder's themes, ``<study>/coders/<id>/themes.json``.
  Another coder's store is opened read-only.
- ``CommonThemeStore``: common themes, kept in ``common/themes.json`` and
  ``common/theme_cards.json`` with newest-wins records, like common codes.

The file records geometry as well as membership: an area's box and a position
for every card. Membership is derived from the cards (a card inside an area is a
code in that theme); ``refs`` is kept in sync for the board. One card per code
per area; a code in two themes has two cards.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .common import COMMON

SCHEMA_VERSION = 2

#: Where a quote that has not been sorted anywhere yet belongs.
UNSORTED = "unsorted"

# -- canvas geometry --------------------------------------------------------
#
# In canvas units, which are CSS pixels at zoom 1. These live here and not only
# in the stylesheet because the server does the *first* layout: a themes file
# written before the canvas existed records which quotes are in a theme but not
# where any of them sits, and those quotes have to land somewhere readable
# before the page is ever opened. Every later position comes from a drag.

CARD_W, CARD_H = 230.0, 150.0
CARD_GAP = 14.0
AREA_W, AREA_H = 520.0, 400.0
#: One column of cards, and enough height to see that an area is empty.
AREA_MIN_W, AREA_MIN_H = 272.0, 220.0
AREA_PAD = 14.0
#: Room at the top of an area for its title, note and buttons.
AREA_HEAD = 84.0
AREA_GAP = 56.0
AREAS_PER_ROW = 3

#: How far from the origin anything may be placed. Not a design limit -- a guard,
#: so a bad number from a client cannot strand an area where no amount of panning
#: will find it again.
CANVAS_LIMIT = 40000.0


def _finite(value, fallback: float = 0.0) -> float:
    """A usable float, whatever the client actually sent."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return number


def _coord(value, fallback: float = 0.0) -> float:
    return round(max(-CANVAS_LIMIT, min(CANVAS_LIMIT, _finite(value, fallback))), 2)


def _extent(value, minimum: float, fallback: float) -> float:
    return round(max(minimum, min(CANVAS_LIMIT, _finite(value, fallback))), 2)


def _has_box(theme: dict) -> bool:
    return all(isinstance(theme.get(key), (int, float)) for key in ("x", "y", "w", "h"))


def _overlaps(a: tuple, b: tuple) -> bool:
    return (
        a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
        and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
    )


def area_columns(width: float = AREA_W) -> int:
    """How many cards fit across an area of this width."""
    return max(1, int((width - 2 * AREA_PAD + CARD_GAP) // (CARD_W + CARD_GAP)))


def _slot(index: int, columns: int) -> tuple[float, float]:
    """The ``index``-th grid position inside an area, in area-local coordinates."""
    return (
        AREA_PAD + (index % columns) * (CARD_W + CARD_GAP),
        AREA_HEAD + (index // columns) * (CARD_H + CARD_GAP),
    )


def _inside(theme: dict, x: float, y: float) -> tuple[float, float]:
    """A position for a card in ``theme``, brought within its box.

    Held at every write rather than only checked on read, so "a card in an area
    is inside that area" is an invariant of the file and not a hope about the
    client. Nothing may sit over the header: the theme's own name is the one
    thing on the canvas that must always be readable.
    """
    right = max(AREA_PAD, theme["w"] - AREA_PAD - CARD_W)
    bottom = max(AREA_HEAD, theme["h"] - AREA_PAD - CARD_H)
    return (
        round(min(max(x, AREA_PAD), right), 2),
        round(min(max(y, AREA_HEAD), bottom), 2),
    )


def _fits(count: int, width: float = AREA_W) -> float:
    """The area height that shows ``count`` cards without scrolling."""
    rows = max(1, -(-max(1, count) // area_columns(width)))
    return max(AREA_H, AREA_HEAD + rows * (CARD_H + CARD_GAP) - CARD_GAP + AREA_PAD)


#: Distinguishes "this card came from nowhere -- add one" from "it came from the
#: bare canvas", which is a real origin and spells itself None.
_KEEP = object()


def packing_key(quote: dict) -> tuple:
    """Where a quote falls when an area is packed into a grid: speaker, then time.

    Who said it first, because a theme read down a column of one voice at a time
    is a theme you can argue with -- the same person's three remarks about trust
    sit together, and the place where somebody else takes over is visible. Then
    time, so each voice runs in the order it was said rather than in the order it
    happened to be dragged out.

    Quotes with nobody attributed sort last: they are the ones to fix, not the
    ones to read first. Recording is the final tiebreak, and does nothing except
    make the result the same every time it is computed.

    The canvas applies the same rule to grid view, in JavaScript. Two spellings
    of one sentence, which is the cheaper mistake: the alternative is asking the
    server to re-sort on every redraw of a view that writes nothing at all.
    """
    speaker = str(quote.get("speaker") or "").strip()
    return (
        0 if speaker else 1,
        speaker.lower(),
        float(quote.get("start_time") or 0.0),
        str(quote.get("recording_id") or ""),
    )


def packing_order(quotes: list[dict]) -> dict[str, tuple]:
    """``packing_key`` for every quote in the library, by reference."""
    return {quote["ref"]: packing_key(quote) for quote in quotes}


def metrics() -> dict:
    """The canvas geometry the page needs, from the one place it is decided.

    The server does the first layout, so it owns these numbers. Handing them to
    the client rather than restating them in the stylesheet is what keeps a card
    the same size in a box the server sized.
    """
    return {
        "card_w": CARD_W,
        "card_h": CARD_H,
        "card_gap": CARD_GAP,
        "area_pad": AREA_PAD,
        "area_head": AREA_HEAD,
        "area_min_w": AREA_MIN_W,
        "area_min_h": AREA_MIN_H,
        "area_w": AREA_W,
        "area_h": AREA_H,
        "limit": CANVAS_LIMIT,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def quote_ref(recording_id: str, highlight_id: str) -> str:
    return f"{recording_id}:{highlight_id}"


def code_names(quote: dict, books) -> list[str]:
    """A quote's text codes as names, read from its coder's codebook.

    The library, themes and analysis views were built on tag names, so they are
    handed names; a code missing from the codebook is skipped.
    """
    if books is None or not quote.get("coder"):
        return []
    codebook = books.text(quote["coder"])
    names = []
    for code_id in quote.get("codes") or []:
        code = codebook.get(code_id)
        if code:
            names.append(code["name"])
    return names


MODES = ("independent", "collaborative")


def code_label(name: str, coder: str, mode: str | None, initials: dict) -> str:
    """How a code is named on the analysis pages.

    In collaborative mode two coders may each have a code called "trust"; they
    stay separate entries, told apart by initials, until they are agreed in common.
    """
    return f"{name} · {initials.get(coder, '?')}" if mode == "collaborative" else name


def all_quotes(registry, mode: str | None = None, coder: str | None = None) -> list[dict]:
    """The library's quotes, with where they came from and their codes by name.

    With no mode, every coder's quotes: theme membership is pruned against this
    list, so a filtered one would delete other coders' quotes from themes. With a
    mode, what that mode shows: the caller's own (independent) or everyone's,
    their codes labelled with initials (collaborative).
    """
    initials = {**({c["id"]: c["initials"] for c in registry.coders.list()} if registry.coders else {}), COMMON: "✓"}
    quotes: list[dict] = []
    for recording in registry.list():
        for highlight in recording.store.list():
            # Common quotes are agreed, so every mode shows them.
            if mode == "independent" and highlight.get("coder") not in (coder, COMMON):
                continue
            names = code_names(highlight, registry.books)
            quotes.append(
                {
                    **highlight,
                    "tags": [code_label(n, highlight.get("coder"), mode, initials) for n in names],
                    "ref": quote_ref(recording.id, highlight["id"]),
                    "recording_id": recording.id,
                    "recording_title": recording.title,
                }
            )
    return quotes


def code_items(registry, mode: str | None = None, coder: str | None = None) -> list[dict]:
    """Every code the mode shows, shaped for the themes page's cards.

    A theme holds codes, and its cards are codes: each item says what the code
    is called (``text``, labelled with initials in collaborative mode), whose it
    is, how many quotes or spans carry it and in which recordings, and those
    applications themselves, so a card can open to show its evidence and play it.
    """
    initials = {**({c["id"]: c["initials"] for c in registry.coders.list()} if registry.coders else {}), COMMON: "✓"}
    uses: dict[str, list[dict]] = {}
    for recording in registry.list():
        for quote in recording.store.list():
            for code_id in quote.get("codes") or []:
                uses.setdefault(f"text:{code_id}", []).append({
                    "ref": quote_ref(recording.id, quote["id"]), "recording_id": recording.id,
                    "recording_title": recording.title, "start_time": quote.get("start_time", 0),
                    "end_time": quote.get("end_time", 0), "text": quote.get("text", ""),
                    "speaker": quote.get("speaker"), "coder": quote.get("coder"),
                })
        for span in recording.video_codes.list():
            uses.setdefault(f"video:{span['code_id']}", []).append({
                "ref": quote_ref(recording.id, span["id"]), "recording_id": recording.id,
                "recording_title": recording.title, "start_time": span["start"], "end_time": span["end"],
                "text": span.get("note") or "", "speaker": None, "coder": span.get("coder"),
            })
    items = []
    for kind, pairs in (("text", registry.books.all_text()), ("video", registry.books.all_video())):
        for owner, code in pairs:
            if mode == "independent" and owner not in (coder, COMMON):
                continue
            ref = f"{kind}:{code['id']}"
            # By speaker, then time; uncredited quotes and video spans last.
            applications = sorted(uses.get(ref, []), key=lambda a: (a["speaker"] is None, (a["speaker"] or "").lower(), a["start_time"], a["recording_title"]))
            items.append({
                "ref": ref, "kind": kind, "id": code["id"], "name": code["name"],
                "text": code_label(code["name"], owner, mode, initials), "color": code.get("color"),
                "description": code.get("description", ""), "coder": owner, "count": len(applications),
                "recordings": sorted({a["recording_id"] for a in applications}), "applications": applications,
            })
    return sorted(items, key=lambda i: (i["text"].lower(), i["kind"]))


def code_packing_order(items: list[dict]) -> dict[str, tuple]:
    """How tidying orders code cards: alphabetically by name, text codes before video."""
    return {i["ref"]: (0, i["name"].lower(), 0 if i["kind"] == "text" else 1, "") for i in items}


def video_index(registry, mode: str | None = None, coder: str | None = None) -> list[dict]:
    """Video codes with their span counts per recording, as the mode shows them."""
    initials = {**({c["id"]: c["initials"] for c in registry.coders.list()} if registry.coders else {}), COMMON: "✓"}
    entries: dict[str, dict] = {}
    for recording in registry.list():
        for span in recording.video_codes.list():
            if mode == "independent" and span.get("coder") not in (coder, COMMON):
                continue
            code = registry.books.video(span["coder"]).get(span["code_id"])
            if code is None:
                continue
            entry = entries.setdefault(
                code["id"],
                {
                    "id": code["id"],
                    "name": code_label(code["name"], span["coder"], mode, initials),
                    "color": code["color"],
                    "coder": span["coder"],
                    "span_count": 0,
                    "recordings": {},
                },
            )
            entry["span_count"] += 1
            entry["recordings"][recording.id] = entry["recordings"].get(recording.id, 0) + 1
    for entry in entries.values():
        entry["recording_count"] = len(entry["recordings"])
    return sorted(entries.values(), key=lambda e: (-e["recording_count"], -e["span_count"], e["name"].lower()))


def tag_index(quotes: list[dict]) -> list[dict]:
    """Per-tag counts, and how they spread across recordings.

    ``recording_count`` is the number that matters most in analysis: a tag on
    twenty quotes from one participant is that person's preoccupation, while the
    same tag across twelve participants is a finding.
    """
    index: dict[str, dict] = {}
    for quote in quotes:
        for tag in quote.get("tags") or []:
            entry = index.setdefault(
                tag, {"tag": tag, "quote_count": 0, "recordings": {}, "colors": {}}
            )
            entry["quote_count"] += 1
            rec = quote["recording_id"]
            entry["recordings"][rec] = entry["recordings"].get(rec, 0) + 1
            color = quote.get("color")
            if color:
                entry["colors"][color] = entry["colors"].get(color, 0) + 1

    summary = []
    for entry in index.values():
        entry["recording_count"] = len(entry["recordings"])
        entry["color"] = (
            max(entry["colors"].items(), key=lambda kv: kv[1])[0] if entry["colors"] else None
        )
        entry.pop("colors")
        summary.append(entry)
    return sorted(summary, key=lambda e: (-e["recording_count"], -e["quote_count"], e["tag"].lower()))


def vocabulary(registry) -> list[dict]:
    """Every coder's text codes, with how widely each is used, for picking while coding.

    Entries keep the ``tag`` key the chip field reads, alongside the code's id,
    colour and coder, so a coder picks their own codes by name.
    """
    uses: dict[str, dict] = {}
    for recording in registry.list():
        for highlight in recording.store.list():
            for code_id in highlight.get("codes") or []:
                entry = uses.setdefault(code_id, {"quote_count": 0, "recordings": []})
                entry["quote_count"] += 1
                if recording.id not in entry["recordings"]:
                    entry["recordings"].append(recording.id)

    entries = []
    for coder, code in registry.books.all_text() if registry.books else []:
        use = uses.get(code["id"], {"quote_count": 0, "recordings": []})
        entries.append(
            {
                **code,
                "tag": code["name"],
                "coder": coder,
                "quote_count": use["quote_count"],
                "recording_count": len(use["recordings"]),
                "recordings": use["recordings"],
            }
        )
    # Most-established first: a code on many recordings is the one to reuse.
    return sorted(entries, key=lambda e: (-e["recording_count"], -e["quote_count"], e["tag"].lower()))


def cooccurrence(quotes: list[dict], minimum: int = 1) -> list[dict]:
    """Tags that share a quote.

    Two codes that always travel together are usually one theme wearing two
    names, or a pair worth reading as cause and effect. Either way it is the
    cheapest signal that a codebook needs consolidating.
    """
    pairs: dict[tuple[str, str], dict] = {}
    for quote in quotes:
        tags = sorted({t for t in (quote.get("tags") or []) if t})
        for i, first in enumerate(tags):
            for second in tags[i + 1 :]:
                key = (first, second)
                entry = pairs.setdefault(
                    key, {"a": first, "b": second, "count": 0, "recordings": set()}
                )
                entry["count"] += 1
                entry["recordings"].add(quote["recording_id"])

    out = []
    for entry in pairs.values():
        if entry["count"] < minimum:
            continue
        entry["recording_count"] = len(entry["recordings"])
        entry.pop("recordings")
        out.append(entry)
    return sorted(out, key=lambda e: (-e["count"], e["a"].lower(), e["b"].lower()))


def untagged(quotes: list[dict]) -> list[dict]:
    return [q for q in quotes if not (q.get("tags") or [])]


# ------------------------------------------------------------------ themes --


@dataclass
class Theme:
    id: str
    title: str
    note: str
    color: str | None
    refs: list[str]


class ThemeStore:
    """Reads and writes the library's themes file.

    Same discipline as the quote store: every change is written immediately and
    atomically, and fields written by a later version survive a round trip.
    """

    def __init__(self, path: Path, readonly: bool = False):
        self.path = path
        #: True when the file exists but could not be read. In a synced study it
        #: may be another coder's, mid-sync, so it is left alone until written.
        self.unreadable = False
        #: Another coder's themes are read, never written: only their own tool
        #: writes their folder. Laying them out still happens, in memory.
        self.readonly = readonly
        self._data = self._load()
        if self._prepare_canvas() and not self.unreadable and not readonly:
            self._write()

    def _empty(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "updated_at": _now(),
            "themes": [],
            "cards": [],
        }

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
        data.setdefault("themes", [])
        if not isinstance(data["themes"], list):
            data["themes"] = []
        if not isinstance(data.get("cards"), list):
            data["cards"] = []
        for theme in data["themes"]:
            theme.setdefault("refs", [])
        return data

    def _write(self) -> None:
        if self.readonly:
            raise PermissionError("another coder's themes are read-only")
        if self.unreadable:
            # Only now, about to write over it, is an unreadable file kept aside.
            try:
                os.replace(self.path, self.path.with_suffix(self.path.suffix + ".corrupt"))
            except OSError:
                pass
            self.unreadable = False
        self._data["updated_at"] = _now()
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

    # -- first canvas layout ---------------------------------------------

    def _prepare_canvas(self) -> bool:
        """Give anything without coordinates somewhere to be.

        A themes file written before the canvas existed says which quotes are in
        a theme and nothing about where they sit. Opening the canvas empty and
        asking for that sorting again would be the worst possible answer, so the
        first read lays the themes out in a grid and packs each one's quotes
        inside it. Cheap, once, and everything afterwards is a drag.

        This also repairs a theme whose cards went missing -- from a hand edit,
        or a crash between two writes. Membership is what matters and ``refs``
        holds it, so a ref with no card gets a card rather than being dropped.
        """
        changed = False
        if self._data.get("version", 0) < SCHEMA_VERSION:
            # Never downgrade: a file from a later version is left saying so.
            self._data["version"] = SCHEMA_VERSION
            changed = True

        homeless = [theme for theme in self._data["themes"] if not _has_box(theme)]
        row_y, row_h = AREA_GAP, 0.0
        for position, theme in enumerate(homeless):
            column = position % AREAS_PER_ROW
            if column == 0 and position:
                row_y += row_h + AREA_GAP
                row_h = 0.0
            height = _fits(len(theme.get("refs") or []))
            theme.update(
                {
                    "x": AREA_GAP + column * (AREA_W + AREA_GAP),
                    "y": row_y,
                    "w": AREA_W,
                    "h": height,
                }
            )
            row_h = max(row_h, height)
            changed = True

        for theme in self._data["themes"]:
            if "collapsed" not in theme:
                theme["collapsed"] = False
                changed = True

        for theme in self._data["themes"]:
            columns = area_columns(theme["w"])
            for index, ref in enumerate(theme.get("refs") or []):
                if self._card(ref, theme["id"]) is not None:
                    continue
                x, y = _slot(index, columns)
                self._data["cards"].append(
                    {"ref": ref, "theme_id": theme["id"], "x": x, "y": y}
                )
                changed = True

        return self._contain() or changed

    def _contain(self) -> bool:
        """Pull every card inside the area that holds it.

        Dragging and resizing both clamp already, so this is for what they cannot
        reach: a hand-edited file, or a position written by a version of this tool
        that clamped differently. A card over its area's header hides the theme's
        own name, which is the one thing on the canvas that must always be
        readable -- so the file is corrected on the way in rather than drawn wrong.
        """
        moved = False
        boxes = {t["id"]: t for t in self._data["themes"] if _has_box(t)}
        for card in self._data["cards"]:
            theme = boxes.get(card.get("theme_id"))
            if theme is None:
                continue
            spot = _inside(theme, _finite(card.get("x")), _finite(card.get("y")))
            if spot != (card.get("x"), card.get("y")):
                card["x"], card["y"] = spot
                moved = True
        return moved

    # -- queries ---------------------------------------------------------

    def list(self) -> list[dict]:
        return self._data["themes"]

    def cards(self) -> list[dict]:
        return self._data["cards"]

    def state(self) -> dict:
        """Everything the canvas draws, in one payload.

        Every canvas edit returns this rather than just what it touched: moving a
        card between areas changes two themes' membership, and a client stitching
        that together from a narrower reply is a client that can drift.
        """
        return {
            "themes": self._data["themes"],
            "cards": self._data["cards"],
            "placed": sorted(self.placed_refs()),
            "on_canvas": sorted(self.on_canvas_refs()),
        }

    def _find(self, theme_id: str) -> dict | None:
        return next((t for t in self._data["themes"] if t.get("id") == theme_id), None)

    def _require(self, theme_id: str) -> dict:
        theme = self._find(theme_id)
        if theme is None:
            raise KeyError(theme_id)
        return theme

    def _card(self, ref: str, theme_id: str | None) -> dict | None:
        return next(
            (
                c
                for c in self._data["cards"]
                if c.get("ref") == ref and c.get("theme_id") == theme_id
            ),
            None,
        )

    def placed_refs(self) -> set[str]:
        """Quotes that are in a theme -- what the board calls sorted."""
        return {ref for theme in self._data["themes"] for ref in theme.get("refs", [])}

    def on_canvas_refs(self) -> set[str]:
        """Quotes with a card anywhere, including loose on the bare canvas.

        Wider than ``placed_refs``: the tray hides a quote once it is out on the
        canvas at all, because a quote you have already pulled out and parked
        next to an area is one you have dealt with, even though no theme claims
        it yet.
        """
        return {card["ref"] for card in self._data["cards"]}

    # -- mutations -------------------------------------------------------

    def create(
        self,
        title: str = "",
        color: str | None = None,
        box: dict | None = None,
        note: str = "",
        cards: list[dict] | None = None,
    ) -> dict:
        """Add a theme, somewhere the canvas can show it.

        ``box`` is where the canvas was clicked. Without one -- a theme made from
        the board, or from a lasso on the map -- it goes in the first grid spot
        that overlaps nothing, so a theme created away from the canvas is still
        findable on it rather than stacked under an existing area.

        ``cards`` puts quotes in it as it is made, at positions given. That exists
        so undoing a deleted area can put the area back exactly as it was in one
        call -- the arrangement inside an area is work, and an undo that restored
        the title but scrambled the contents would not be an undo.
        """
        width = _extent((box or {}).get("w"), AREA_MIN_W, AREA_W)
        height = _extent((box or {}).get("h"), AREA_MIN_H, AREA_H)
        if box and box.get("x") is not None and box.get("y") is not None:
            x, y = _coord(box["x"]), _coord(box["y"])
        else:
            x, y = self._vacancy(width, height)

        theme = {
            "id": uuid.uuid4().hex[:12],
            "title": title.strip() or "Untitled theme",
            "note": str(note or ""),
            "color": color,
            "refs": [],
            "x": x,
            "y": y,
            "w": width,
            "h": height,
            "collapsed": False,
            "created_at": _now(),
            "updated_at": _now(),
        }
        self._data["themes"].append(theme)

        for position, card in enumerate(cards or []):
            ref = str(card.get("ref") or "")
            if not ref or self._card(ref, theme["id"]) is not None:
                continue
            fallback = _slot(position, area_columns(width))
            self._data["cards"].append(
                {
                    "ref": ref,
                    "theme_id": theme["id"],
                    "x": _coord(card.get("x"), fallback[0]),
                    "y": _coord(card.get("y"), fallback[1]),
                }
            )
        if cards:
            # Cards given with the theme are kept inside it, as a drop would be.
            self._contain()
            self._sync_refs()
        self._write()
        return theme

    def _vacancy(self, width: float, height: float) -> tuple[float, float]:
        """A grid spot on the canvas where a new area lands on nothing."""
        boxes = [
            (t["x"], t["y"], t["w"], t["h"]) for t in self._data["themes"] if _has_box(t)
        ]
        for slot in range(2 * len(boxes) + AREAS_PER_ROW + 1):
            x = AREA_GAP + (slot % AREAS_PER_ROW) * (AREA_W + AREA_GAP)
            y = AREA_GAP + (slot // AREAS_PER_ROW) * (AREA_H + AREA_GAP)
            if not any(_overlaps((x, y, width, height), box) for box in boxes):
                return x, y
        return AREA_GAP, AREA_GAP

    def update(self, theme_id: str, patch: dict) -> dict:
        theme = self._require(theme_id)
        if "title" in patch:
            theme["title"] = str(patch["title"]).strip() or "Untitled theme"
        if "note" in patch:
            theme["note"] = str(patch["note"] or "")
        if "color" in patch:
            theme["color"] = patch["color"] or None
        if "collapsed" in patch:
            # Rolled up to its title, keeping its size and everything in it. A
            # study's themes are not all live at once, and a finished one taking
            # up a screenful of plane is a finished one in the way.
            theme["collapsed"] = bool(patch["collapsed"])
        theme["updated_at"] = _now()
        self._write()
        return theme

    def delete(self, theme_id: str) -> bool:
        theme = self._find(theme_id)
        if theme is None:
            return False
        self._data["themes"].remove(theme)
        # The cards go with it. They were positioned inside the area, so leaving
        # them behind would strand them at coordinates relative to a box that no
        # longer exists -- the quotes return to the tray instead.
        self._data["cards"] = [
            card for card in self._data["cards"] if card.get("theme_id") != theme_id
        ]
        self._write()
        return True

    def reorder(self, order: list[str]) -> list[dict]:
        position = {theme_id: index for index, theme_id in enumerate(order)}
        self._data["themes"].sort(key=lambda t: position.get(t["id"], len(position)))
        self._write()
        return self._data["themes"]

    def reshape(self, theme_id: str, box: dict) -> dict:
        """Move or resize an area.

        Card positions inside an area are relative to it, so moving one is a
        single number changing and every quote in it comes along -- which is the
        whole reason they are stored that way. Resizing has to answer for the
        cards a smaller box no longer covers, and does it by pulling them back
        inside rather than evicting them: a quote does not stop being part of a
        theme because the box around it was dragged in.
        """
        theme = self._require(theme_id)
        if "x" in box:
            theme["x"] = _coord(box["x"], theme["x"])
        if "y" in box:
            theme["y"] = _coord(box["y"], theme["y"])
        if "w" in box:
            theme["w"] = _extent(box["w"], AREA_MIN_W, theme["w"])
        if "h" in box:
            theme["h"] = _extent(box["h"], AREA_MIN_H, theme["h"])

        for card in self._data["cards"]:
            if card.get("theme_id") == theme_id:
                card["x"], card["y"] = _inside(theme, card["x"], card["y"])

        theme["updated_at"] = _now()
        self._write()
        return theme

    def place(
        self,
        ref: str,
        theme_id: str | None,
        x,
        y,
        moved_from: str | None | object = _KEEP,
    ) -> dict:
        """Put a card for ``ref`` at (x, y), in ``theme_id`` or loose on the canvas.

        Coordinates are relative to the area for a card in one, and absolute for
        a loose card.

        ``moved_from`` names the card this one came from, so dragging a card out
        of one area and into another moves it instead of leaving a copy behind.
        Left out, this *adds* a card -- which is how one quote comes to sit in two
        themes. There is only ever one card per quote per area, so placing into
        somewhere the quote already is just moves it.

        Callers that have no position to give -- the board, where a column has no
        coordinates to offer -- leave it out, and a card new to the area lands in
        the first free grid slot rather than on top of whatever is at the corner.
        A card already there keeps where it is.
        """
        theme = self._require(theme_id) if theme_id is not None else None
        if moved_from is not _KEEP and moved_from != theme_id:
            self._forget(ref, moved_from)

        card = self._card(ref, theme_id)
        if card is None:
            fallback = self._make_room(theme) if theme else (0.0, 0.0)
            card = {"ref": ref, "theme_id": theme_id, "x": fallback[0], "y": fallback[1]}
            self._data["cards"].append(card)
        card["x"], card["y"] = self._at(theme, _coord(x, card["x"]), _coord(y, card["y"]))

        self._sync_refs()
        self._write()
        return self.state()

    def unplace(self, ref: str, theme_id: str | None) -> dict:
        """Take one card off the canvas, leaving any others for the same quote."""
        self._forget(ref, theme_id)
        self._sync_refs()
        self._write()
        return self.state()

    @staticmethod
    def _at(theme: dict | None, x: float, y: float) -> tuple[float, float]:
        """Where a card may sit: inside its area, or anywhere at all when loose."""
        return _inside(theme, x, y) if theme else (x, y)

    def _forget(self, ref: str, theme_id: str | None) -> None:
        card = self._card(ref, theme_id)
        if card is not None:
            self._data["cards"].remove(card)

    def reposition(self, moves: list[dict]) -> dict:
        """Set several card positions at once.

        Arranging post-its is a lot of small movements. Sending them together
        means a drag that ends up touching four cards is one write to the file,
        not four -- and no window where the file records half of it.
        """
        for move in moves:
            theme_id = move.get("theme_id") or None
            card = self._card(str(move.get("ref") or ""), theme_id)
            if card is None:
                continue
            card["x"], card["y"] = self._at(
                self._find(theme_id) if theme_id else None,
                _coord(move.get("x"), card["x"]),
                _coord(move.get("y"), card["y"]),
            )
        self._write()
        return self.state()

    def tidy(self, theme_id: str, ranking: dict[str, tuple] | None = None) -> dict:
        """Pack an area's cards into a grid, and grow it to fit them.

        Free placement is the point of the canvas, and it is also how an area
        ends up with two cards on top of each other and a third off the bottom
        edge. This is the way back, per area, without undoing the sorting.

        ``ranking`` is ``packing_order``: speaker, then time. Tidying is the
        moment you have decided that where the cards happen to sit means
        nothing, so it puts them in an order that means something instead of
        preserving the arrangement you just gave up on. It is also the order grid
        view draws, which is what lets that view be a true preview of this.

        Without a ranking, position falls back to reading order -- an ordering
        that at least does not shuffle a tidy area on a second press.
        """
        theme = self._require(theme_id)
        mine = [c for c in self._data["cards"] if c.get("theme_id") == theme_id]
        if ranking:
            # A card whose quote has gone sorts last; prune will take it shortly.
            mine.sort(key=lambda c: ranking.get(c["ref"], (2, "", 0.0, "")))
        else:
            mine.sort(key=lambda c: (round(c["y"] / (CARD_H / 2)), c["x"]))
        theme["h"] = _fits(len(mine), theme["w"])
        columns = area_columns(theme["w"])
        for index, card in enumerate(mine):
            card["x"], card["y"] = _slot(index, columns)
        theme["updated_at"] = _now()
        self._sync_refs()
        self._write()
        return self.state()

    def assign(self, ref: str, theme_id: str | None, index: int | None = None) -> list[dict]:
        """Move a quote into one theme, or out of every theme.

        The board's move, and the map's: pick a theme and the quote leaves
        whichever others held it. The canvas is where a quote goes into two
        themes at once, because there you can see that it did -- a select box
        showing one theme could not say so, and would quietly undo the second.
        """
        for card in [c for c in self._data["cards"] if c.get("ref") == ref]:
            self._data["cards"].remove(card)

        if theme_id is not None:
            theme = self._require(theme_id)
            x, y = self._make_room(theme)
            self._data["cards"].append({"ref": ref, "theme_id": theme_id, "x": x, "y": y})
            theme["updated_at"] = _now()

        self._sync_refs()
        if theme_id is not None and index is not None:
            # The board can say where in its column the quote landed. Order is
            # the column's own, so it is applied after membership is derived.
            refs = self._require(theme_id)["refs"]
            refs.remove(ref)
            refs.insert(max(0, min(index, len(refs))), ref)
        self._write()
        return self._data["themes"]

    def _make_room(self, theme: dict) -> tuple[float, float]:
        """Clear a spot in an area for a card, growing the area if it has to.

        A quote can arrive in a theme without anyone saying where to put it --
        moved on the board, lassoed on the map. It still needs coordinates, and
        dropping it on top of a card already there would hide both of them.

        Overlap, not an exact match: cards sit where a hand left them, so a slot
        can be free of any card's *corner* and still be entirely underneath one.

        And the area gives way rather than the card. Every position is held
        inside the box that owns it, so squeezing a card back in would put it
        straight back on top of something -- an area out of room grows, which is
        what a person would do with the pen.
        """
        columns = area_columns(theme["w"])
        taken = [
            (c["x"], c["y"], CARD_W, CARD_H)
            for c in self._data["cards"]
            if c.get("theme_id") == theme["id"]
        ]
        spot = None
        for slot in range(len(taken) + 1):
            x, y = _slot(slot, columns)
            if not any(_overlaps((x, y, CARD_W, CARD_H), box) for box in taken):
                spot = (x, y)
                break
        if spot is None:
            # Every slot is under something: start a fresh row below the lot.
            spot = _slot(-(-(len(taken) + 1) // columns) * columns, columns)

        theme["h"] = round(max(theme["h"], spot[1] + CARD_H + AREA_PAD), 2)
        return spot

    def _sync_refs(self) -> None:
        """Make each theme's ref list say exactly what its cards say.

        Membership is a property of the cards: a card inside an area is a quote
        in that theme. ``refs`` is that same fact in the shape the board reads,
        so it is derived here rather than maintained alongside -- one fact, one
        place it is decided. Existing order is kept, because the board lets you
        order a column and re-deriving must not shuffle it.
        """
        for theme in self._data["themes"]:
            mine = {c["ref"] for c in self._data["cards"] if c.get("theme_id") == theme["id"]}
            ordered = [ref for ref in theme.get("refs") or [] if ref in mine]
            ordered += [
                c["ref"]
                for c in self._data["cards"]
                if c.get("theme_id") == theme["id"] and c["ref"] not in ordered
            ]
            theme["refs"] = ordered

    def replace_ref(self, old: str, new: str) -> int:
        """Point every card for ``old`` at ``new``, e.g. when a code moves to common.

        A theme that already holds ``new`` keeps its one card for it.
        """
        changed = 0
        for card in list(self._data["cards"]):
            if card.get("ref") != old:
                continue
            changed += 1
            if self._card(new, card.get("theme_id")):
                self._data["cards"].remove(card)
            else:
                card["ref"] = new
        for theme in self._data["themes"]:
            renamed = [new if r == old else r for r in theme.get("refs", [])]
            theme["refs"] = list(dict.fromkeys(renamed))
        if changed:
            self._sync_refs()
            self._write()
        return changed

    def remove_ref(self, ref: str) -> int:
        """Take a code out of every theme and off the canvas, e.g. when it is deleted."""
        cards = [c for c in self._data["cards"] if c.get("ref") != ref]
        removed = len(self._data["cards"]) - len(cards)
        if removed:
            self._data["cards"] = cards
            self._sync_refs()
            self._write()
        return removed

    def prune(self, known: set[str]) -> int:
        """Drop references to quotes that no longer exist.

        A quote deleted in the reader would otherwise leave a hole in a theme
        that nothing accounts for, and a card on the canvas with nothing to draw.
        """
        cards = [card for card in self._data["cards"] if card.get("ref") in known]
        removed = len(self._data["cards"]) - len(cards)
        for theme in self._data["themes"]:
            keep = [ref for ref in theme["refs"] if ref in known]
            removed += len(theme["refs"]) - len(keep)
            theme["refs"] = keep
        if removed:
            self._data["cards"] = cards
            self._sync_refs()
            self._write()
        return removed


# -- common themes --------------------------------------------------------------


def _card_key(card: dict) -> str:
    return f"{card.get('theme_id') or 'loose'}|{card['ref']}"


class CommonThemeStore(ThemeStore):
    """The agreed themes, which anyone may edit, kept like other common records.

    A theme and each of its cards are separate common records, so two coders
    arranging the same theme at once each change only their own records and
    neither overwrites the other. Every change is written to the acting coder's
    own copy (set ``writer`` before changing anything); the store works out what
    changed by comparing with what it last read or wrote.
    """

    THEMES = "themes.json"
    CARDS = "theme_cards.json"

    def __init__(self, root: Path):
        from .common import CommonSet

        self.path = Path(root) / "common" / self.THEMES
        self.unreadable = False
        self.readonly = False
        self.writer: str | None = None
        self._themes = CommonSet(root, self.THEMES)
        self._cards = CommonSet(root, self.CARDS)
        self._data = self._load()

    @property
    def incomplete(self) -> bool:
        return self._themes.incomplete or self._cards.incomplete

    def sets(self):
        return (self._themes, self._cards)

    def reload(self) -> None:
        self._themes.reload()
        self._cards.reload()
        self._data = self._load()

    @staticmethod
    def _clean(record: dict) -> dict:
        return {k: v for k, v in record.items() if k not in ("updated_at", "updated_by", "deleted")}

    def _load(self) -> dict:
        themes = [{**self._clean(t), "refs": []} for t in self._themes.list()]
        ids = {t["id"] for t in themes}
        cards = [self._clean(c) for c in self._cards.list() if not c.get("theme_id") or c["theme_id"] in ids]
        for card in cards:
            card.pop("id", None)
        self._seen = self._snapshot(themes, cards)
        data = {"version": SCHEMA_VERSION, "updated_at": _now(), "themes": themes, "cards": cards}
        self._data = data
        self._sync_refs()
        return data

    @staticmethod
    def _snapshot(themes, cards):
        return (
            {t["id"]: {k: v for k, v in t.items() if k != "refs"} for t in themes},
            {_card_key(c): dict(c) for c in cards},
        )

    def _write(self) -> None:
        if not self.writer:
            raise RuntimeError("common themes are changed on behalf of a coder; set writer first")
        themes, cards = self._snapshot(self._data["themes"], self._data["cards"])
        seen_themes, seen_cards = self._seen
        for theme_id, theme in themes.items():
            if seen_themes.get(theme_id) != theme:
                self._themes.put(self.writer, theme)
        for theme_id in set(seen_themes) - set(themes):
            self._themes.delete(self.writer, theme_id)
        for key, card in cards.items():
            if seen_cards.get(key) != card:
                self._cards.put(self.writer, {"id": key, **card})
        for key in set(seen_cards) - set(cards):
            self._cards.delete(self.writer, key)
        self._seen = (themes, cards)
