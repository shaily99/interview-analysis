"""Correcting the transcript in place.

Two things make this more than a file write.

The original is preserved. Before the first edit to a transcript, it is copied
to ``<name>_original.vtt``. That copy is written once and never touched again,
so it always holds the file as it came off Zoom no matter how many corrections
follow.

Edits are spliced, not regenerated. Only the edited cue's payload is replaced in
the source text; every other byte is left exactly as it was. Regenerating the
file from parsed data would quietly drop anything the parser skipped -- a
malformed timestamp, an unrecognized block -- and those are precisely the parts
of a transcript a person cannot afford to lose silently.

And because corrections happen while quotes are being saved, an edit re-anchors
any quote overlapping the cue rather than letting its offsets drift.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import tempfile
from pathlib import Path

from .models import Cue
from .timings import split_words, word_index_of
from .vtt import (
    merge_cue_blocks,
    merged_payload,
    read_speakers,
    splice_cue,
    splice_speaker,
    split_cue_block,
    write_roster,
)


class EditError(ValueError):
    """Raised when an edit cannot be applied."""


#: Appended to a transcript's stem for its pre-edit backup. Discovery skips
#: files ending in this, or a backup would be read as another recording.
BACKUP_SUFFIX = "_original"


def backup_path(vtt_path: Path) -> Path:
    return vtt_path.with_name(f"{vtt_path.stem}{BACKUP_SUFFIX}{vtt_path.suffix}")


def ensure_backup(vtt_path: Path) -> str | None:
    """Copy the transcript aside once. Returns the name if it was just created.

    Deliberately never overwrites: the backup is the file as it arrived, not the
    state before the most recent edit.
    """
    target = backup_path(vtt_path)
    if target.exists():
        return None
    shutil.copy2(vtt_path, target)
    return target.name


def read_source(path: Path) -> str:
    """Read a transcript without translating line endings.

    Universal newlines would turn a CRLF file into LF in memory, and writing it
    back would silently reformat every line of the user's transcript. Offsets are
    tracked against the file exactly as it is on disk.
    """
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def write_atomically(path: Path, content: str) -> None:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def remap_offset(old: str, new: str, offset: int) -> int:
    """Carry a character offset from the old text onto the corrected text.

    A quote anchored inside a cue has to survive that cue being edited. Offsets
    in untouched regions move by exactly the shift ahead of them; offsets inside
    a rewritten region collapse to whichever edge of the replacement is nearer,
    since there is no honest sub-position to map them to.
    """
    offset = max(0, min(offset, len(old)))
    # Half-open ranges give the anchor forward gravity: an offset sitting exactly
    # where text was inserted belongs to the word that follows it, not to the
    # insertion point, so a quote keeps covering the same words.
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if not i1 <= offset < i2:
            continue
        if tag == "equal":
            return j1 + (offset - i1)
        return j1 if (offset - i1) <= (i2 - offset) else j2
    return len(new)


_WHITESPACE_RE = re.compile(r"\s+")


def normalize_edit(text: str) -> str:
    """A cue is one line of the source, so newlines and runs of space collapse."""
    return _WHITESPACE_RE.sub(" ", text or "").strip()


#: Keys the reader already uses. A speaker cannot take one, or naming somebody
#: would quietly break navigation.
RESERVED_KEYS = set("jkechfs/[]ioxm")


def apply_roster_edit(recording, entries: list[dict]) -> dict:
    """Rewrite the roster of speakers and their keys.

    The roster lives in the transcript, but nobody should have to open the
    transcript to change it. Every part of a session gets the same roster, since
    the speakers are the same people either side of an interruption.
    """
    cleaned: list[dict] = []
    seen_names: set[str] = set()
    seen_keys: set[str] = set()

    for entry in entries:
        name = normalize_edit(str(entry.get("name") or ""))
        if not name:
            continue
        if ":" in name:
            raise EditError("a speaker's name cannot contain a colon")
        if name.lower() in seen_names:
            raise EditError(f"{name} is listed twice")
        seen_names.add(name.lower())

        key = str(entry.get("key") or "").strip()
        if key:
            if len(key) != 1:
                raise EditError(f"a key is a single character, not {key!r}")
            if key in RESERVED_KEYS:
                raise EditError(f"{key} is already a reader shortcut; pick another")
            if key in seen_keys:
                raise EditError(f"two speakers cannot share the key {key}")
            seen_keys.add(key)
        cleaned.append({"key": key or None, "name": name})

    for index, files in enumerate(recording.part_files):
        content = recording.sources[index]
        ensure_backup(files.vtt_path)
        content = write_roster(content, cleaned)
        write_atomically(files.vtt_path, content)
        recording.sources[index] = content

    recording.reload_transcript()
    return {"roster": recording.transcript.roster}


def apply_cue_split(
    recording, cue_id: str, offset: int, against: str | None = None, align: bool = True
) -> dict:
    """Cut one caption into two at a point inside its text.

    The case this is for: Zoom puts the end of one person's turn and the start of
    another's in a single caption. Reassigning whole cues cannot separate them,
    so the caption has to become two before either half can be attributed.

    The boundary time is interpolated across the caption, the same estimate the
    reader uses for a quote -- honest about being an estimate, and close enough
    that playing either half lands on the right words.
    """
    transcript = recording.transcript
    cue: Cue | None = transcript.cue(cue_id)
    if cue is None:
        raise EditError("that line is not part of this transcript")

    offset = int(offset)
    if against is not None and against != cue.text:
        # The caret was placed in the editor, whose text may differ from what is
        # stored -- unsaved typing, or whitespace the file collapses. Carry the
        # offset across that difference rather than cutting at the wrong word.
        offset = remap_offset(against, cue.text, offset)

    head = normalize_edit(cue.text[:offset])
    tail = normalize_edit(cue.text[offset:])
    if not head or not tail:
        raise EditError("a split needs words on both sides of the cut")

    # Measure this one caption first, if it has not been. A cut is exactly where
    # interpolation is least defensible -- it becomes a timestamp in the file that
    # outlives the decision -- so it is worth a second of audio work to place it
    # on the real silence instead of a guess.
    if align and not cue.timed:
        from .alignment import AlignmentError

        try:
            if recording.measure([cue]):
                cue = recording.transcript.cue(cue_id) or cue
        except AlignmentError:
            pass  # no model, no ffmpeg, no audio: interpolate as before

    part_index = cue.part_index
    vtt_path = recording.part_files[part_index].vtt_path

    # Where the words have been aligned, the cut lands in the measured silence
    # between them and the halves keep their own edges. Otherwise both share one
    # interpolated boundary, which is a guess and is marked as one.
    frame = cue.boundary_at_offset(offset)
    measured = frame is not None
    at, tail_at = frame if frame else (cue.time_at_offset(offset),) * 2

    # Never produce a zero-length half; a caption of no duration cannot be played.
    floor, ceiling = cue.start + 0.001, cue.end - 0.001
    at = min(max(at, floor), ceiling)
    tail_at = min(max(tail_at, at), ceiling)

    part = transcript.part(part_index)
    backup_created = ensure_backup(vtt_path)
    content = split_cue_block(
        recording.sources[part_index],
        cue,
        offset,
        at,
        part.offset if part else 0.0,
        tail_at,
    )
    write_atomically(vtt_path, content)
    recording.sources[part_index] = content
    recording.reload_transcript()

    raw_tail = cue.text[offset:]
    touched = recording.store.remap_split(
        cue.index, offset, len(head), len(raw_tail) - len(raw_tail.lstrip())
    )

    return {
        "changed": True,
        "cue_ids": [f"c{cue.index}", f"c{cue.index + 1}"],
        # The two halves as written, so undoing this can prove it is putting back
        # the same words rather than whichever captions now hold those numbers.
        "halves": [head, tail],
        "at": round(at, 3),
        "tail_at": round(tail_at, 3),
        "measured": measured,
        "backup_created": backup_created,
        "highlights": touched,
    }


def snap_to_words(text: str, start: int, end: int) -> tuple[int, int]:
    """Widen a span to whole words.

    A caption boundary inside a word leaves two fragments, so a selection that
    began or ended mid-word takes the whole word with it. A selection already on
    word edges is left exactly where it is.
    """
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    return start, end


def apply_selection_speaker(
    recording,
    start_cue_id: str,
    start_offset: int,
    end_cue_id: str,
    end_offset: int,
    speaker: str,
) -> dict:
    """Give a selected passage to a different speaker, cutting captions to fit it.

    The gesture this serves: reading along, you see that half of what Zoom filed
    under one person was actually said by the other. Select those words and hand
    them over. Whatever captions have to be divided to make that possible are
    divided, which is usually one caption into three -- what came before, the
    passage itself, and what came after.

    Built out of the two existing operations rather than a third way of editing the
    file, so a selection handed over gets the same backup, the same re-anchored
    quotes, and the same measured caption boundaries as a cut made by hand.
    """
    transcript = recording.transcript
    first = transcript.cue(start_cue_id)
    last = transcript.cue(end_cue_id)
    if first is None or last is None:
        raise EditError("that selection is not part of this transcript")
    if first.index > last.index:
        first, last = last, first
        start_offset, end_offset = end_offset, start_offset
    if first.part_index != last.part_index:
        raise EditError("a selection spanning two recordings cannot be reattributed")

    speaker = normalize_edit(speaker)
    if not speaker:
        raise EditError("choose who said it")

    if first.id == last.id and start_offset == end_offset:
        raise EditError("select some words to hand over")

    # Snap outward, so no caption is left holding half a word.
    if first.id == last.id:
        start_offset, end_offset = snap_to_words(first.text, start_offset, end_offset)
    else:
        start_offset, _ = snap_to_words(first.text, start_offset, start_offset)
        _, end_offset = snap_to_words(last.text, end_offset, end_offset)

    if first.id == last.id and start_offset >= end_offset:
        raise EditError("select some words to hand over")

    # Only cut where there is something on the other side of the cut to keep.
    head_split = bool(first.text[:start_offset].strip())
    tail_split = bool(last.text[end_offset:].strip())
    before, after = first.index, last.index

    touched: dict[str, dict] = {}
    backup: str | None = None
    measured: list[bool] = []

    # The tail first: it inserts a caption after ``last``, which leaves the index
    # of ``first`` alone. Cutting the head first would move ``last`` underneath us.
    if tail_split:
        result = apply_cue_split(recording, f"c{after}", end_offset)
        backup = backup or result["backup_created"]
        measured.append(result["measured"])
        touched.update({h["id"]: h for h in result["highlights"]})
    if head_split:
        result = apply_cue_split(recording, f"c{before}", start_offset)
        backup = backup or result["backup_created"]
        measured.append(result["measured"])
        touched.update({h["id"]: h for h in result["highlights"]})

    # A head cut inserts a caption at ``before + 1``, so everything from the
    # selection onwards has moved along by one.
    shift = 1 if head_split else 0
    named = apply_speaker_edit(
        recording, f"c{before + shift}", speaker, f"c{after + shift}"
    )
    touched.update({h["id"]: h for h in named["highlights"]})

    return {
        "changed": True,
        "speaker": speaker,
        "cue_ids": named["cue_ids"],
        "splits": len(measured),
        "measured": all(measured) if measured else None,
        "backup_created": backup or named["backup_created"],
        "highlights": list(touched.values()),
        "speakers": recording.transcript.speakers,
    }


#: A guard, not a policy: joining a whole transcript into one caption is never
#: what anyone meant, and a runaway request should not be silently obeyed.
MAX_MERGE = 50


def apply_cue_merge(
    recording, cue_id: str, through_cue_id: str, expect: list[str] | None = None
) -> dict:
    """Join a run of consecutive captions into one.

    Two jobs. It undoes a split -- the halves go back together, which is what makes
    an accidental cut recoverable rather than something to repair by hand. And it
    fixes Zoom's opposite failure, one sentence chopped across three captions, where
    a quote that reads as a single thought is three anchors underneath.

    ``expect`` is the texts the caller believed it was joining. Undo is offered in a
    toast, cue ids are positional, and anything can have happened in between -- so
    if the transcript has moved underneath, this refuses rather than joining
    whichever captions now hold those numbers.
    """
    transcript = recording.transcript
    first = transcript.cue(cue_id)
    last = transcript.cue(through_cue_id)
    if first is None or last is None:
        raise EditError("those lines are not part of this transcript")
    if first.index > last.index:
        first, last = last, first
    if first.index == last.index:
        raise EditError("a join needs two captions, not one")
    if first.part_index != last.part_index:
        raise EditError("captions from two different recordings cannot be joined")
    if last.index - first.index + 1 > MAX_MERGE:
        raise EditError(f"that is more than {MAX_MERGE} captions to join at once")

    part_index = first.part_index
    targets = [
        cue
        for cue in transcript.cues
        if cue.part_index == part_index and first.index <= cue.index <= last.index
    ]

    if expect is not None and [normalize_edit(t) for t in expect] != [
        normalize_edit(cue.text) for cue in targets
    ]:
        raise EditError("the transcript has changed since then; nothing was joined")

    merged, starts, lengths = merged_payload(targets)
    if not merged:
        raise EditError("joining those would leave a caption with no words")

    # The label that survives is the first one, since one caption carries one
    # speaker. Reported back, so the interface can say so when they differed.
    speakers = [cue.speaker for cue in targets if cue.speaker]
    absorbed = [name for name in speakers[1:] if name != speakers[0]] if speakers else []

    vtt_path = recording.part_files[part_index].vtt_path
    part = transcript.part(part_index)
    backup_created = ensure_backup(vtt_path)
    content = merge_cue_blocks(
        recording.sources[part_index], targets, part.offset if part else 0.0
    )
    write_atomically(vtt_path, content)
    recording.sources[part_index] = content
    recording.reload_transcript()

    # Word timings need no attention at all: joining leaves the part's sequence of
    # words identical, so every measurement still describes the same word.
    touched = recording.store.remap_merge(first.index, starts, lengths)

    return {
        "changed": True,
        "cue_id": f"c{first.index}",
        "joined": len(targets),
        "speaker": speakers[0] if speakers else None,
        "absorbed_speakers": absorbed,
        "backup_created": backup_created,
        "highlights": touched,
    }


def apply_speaker_edit(
    recording, cue_id: str, speaker: str, through_cue_id: str | None = None
) -> dict:
    """Reattribute a line, or a run of them, to a different speaker.

    Zoom segments badly: a trailing clause routinely lands under whoever spoke
    before it, and an in-person recording files the whole room under one name.
    Both are fixed the same way -- say who actually said this -- so this takes a
    range rather than a single line, because a mis-segmented answer is usually
    several captions long.

    The name is written into the transcript as a normal speaker label *and* into
    a NOTE roster, so re-parsing honours it even when it is a single word on a
    single line, which detection would never accept on its own.
    """
    transcript = recording.transcript
    first = transcript.cue(cue_id)
    last = transcript.cue(through_cue_id) if through_cue_id else first
    if first is None or last is None:
        raise EditError("those lines are not part of this transcript")
    if first.index > last.index:
        first, last = last, first
    if first.part_index != last.part_index:
        raise EditError("a run of lines cannot span two recordings")

    speaker = normalize_edit(speaker)
    if not speaker:
        raise EditError("a speaker needs a name")
    if ":" in speaker or "\n" in speaker:
        raise EditError("a speaker's name cannot contain a colon")
    # A known speaker typed in another case is that speaker, spelled as known.
    known = [e["name"] for e in transcript.roster] + transcript.speakers
    speaker = next((n for n in known if n.lower() == speaker.lower()), speaker)

    part_index = first.part_index
    vtt_path = recording.part_files[part_index].vtt_path
    content = recording.sources[part_index]
    style = transcript.speaker_detection

    targets = [
        cue
        for cue in transcript.cues
        if cue.part_index == part_index and first.index <= cue.index <= last.index
    ]
    if not targets:
        raise EditError("no lines in that range")

    backup_created = ensure_backup(vtt_path)

    # Back to front, so each splice leaves the offsets ahead of it untouched.
    for cue in sorted(targets, key=lambda c: c.source_start, reverse=True):
        content = splice_speaker(content, cue, speaker, style)

    entries = read_speakers(content)
    if not any(entry["name"].lower() == speaker.lower() for entry in entries):
        entries.append({"key": None, "name": speaker})
    content = write_roster(content, entries)

    write_atomically(vtt_path, content)
    recording.sources[part_index] = content
    recording.reload_transcript()

    # Quotes anchored in these captions were credited to whoever used to be on
    # them. Saying who said it has to reach the quotes, not just the transcript.
    touched = recording.store.restate_speaker([cue.id for cue in targets], speaker)

    return {
        "changed": True,
        "speaker": speaker,
        "cue_ids": [cue.id for cue in targets],
        "backup_created": backup_created,
        "backup_file": backup_path(vtt_path).name,
        "highlights": touched,
        "speakers": recording.transcript.speakers,
    }


def apply_cue_edit(recording, cue_id: str, raw_text: str) -> dict:
    """Correct one cue, save it to the transcript, and re-anchor its quotes."""
    transcript = recording.transcript
    cue: Cue | None = transcript.cue(cue_id)
    if cue is None:
        raise EditError("that line is not part of this transcript")

    new_text = normalize_edit(raw_text)
    if not new_text:
        raise EditError("a line cannot be left empty; delete the words but keep the line")

    if new_text == cue.text:
        return {
            "cue": cue.to_dict(),
            "changed": False,
            "backup_created": None,
            "highlights": [],
            "sha256": transcript.sha256,
        }

    part_index = cue.part_index
    try:
        vtt_path = recording.part_files[part_index].vtt_path
        content = recording.sources[part_index]
    except IndexError as exc:
        raise EditError("cannot locate the transcript this line came from") from exc

    # Where this caption's words sit in the part's sequence, which is the index
    # space the measured timings are addressed in. Read before anything changes.
    part_cues = transcript.cues_in_part(part_index)
    vtt_name = vtt_path.name
    first_word = word_index_of(part_cues, cue_id)
    was_words = len(split_words(cue.text))
    now_words = len(split_words(new_text))

    backup_created = ensure_backup(vtt_path)
    updated_content = splice_cue(content, cue, new_text)
    write_atomically(vtt_path, updated_content)
    recording.sources[part_index] = updated_content

    # The replacement changes length, so every later cue in the same file moves.
    old_text = cue.text
    old_source_end = cue.source_end
    new_source_end = cue.source_start + len(cue.prefix) + len(new_text) + len(cue.suffix)
    delta = new_source_end - old_source_end

    transcript.replace_cue(cue.edited(new_text, new_source_end))
    if delta:
        for other in list(transcript.cues_in_part(part_index)):
            if other.id != cue_id and other.source_start >= old_source_end:
                transcript.replace_cue(other.moved(delta))

    # This caption's own words were re-worded, so its measurements are void; the
    # ones after it are still good but have moved along the sequence.
    recording.timings.drop(vtt_name, first_word, first_word + was_words - 1)
    recording.timings.shift(vtt_name, first_word + was_words, now_words - was_words)

    recording.restamp_part(part_index, updated_content)
    touched = recording.store.remap_cue(cue_id, old_text, new_text)
    recording.store.restamp()

    return {
        "cue": transcript.cue(cue_id).to_dict(),
        "changed": True,
        "backup_created": backup_created,
        "backup_file": backup_path(vtt_path).name,
        "highlights": touched,
        "sha256": transcript.sha256,
    }
