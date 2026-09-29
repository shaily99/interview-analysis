"""WebVTT parsing and speaker-aware chunking.

Zoom writes the speaker two different ways: inlined in the cue payload as
``Name: text``, or as a WebVTT ``<v Name>`` voice tag. Voice tags are
unambiguous. The colon form is not -- on a single line, a sentence like
"So here's my point: I disagreed" is indistinguishable from a speaker prefix.

So speaker detection is a whole-file decision, not a per-line guess: candidate
prefixes are collected across the entire transcript first, and a candidate is
only promoted to a speaker if it either looks like a proper name or recurs.
See ``_accept_speakers``. Names that differ only in case are one speaker
(``unify_speakers``).

Each cue also records where its payload sits in the source file, and the exact
prefix and suffix that were stripped from it. That is what lets a correction be
spliced back into the original bytes rather than regenerating the file -- so
anything the parser skipped (a malformed timestamp, an unusual block) survives
an edit untouched.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path

from .models import Chunk, Cue, Part, Transcript

#: Insert a paragraph break inside a chunk when the speaker pauses this long.
PARAGRAPH_GAP = 2.5

#: When no speakers are detected at all, start a new chunk after this much silence.
FALLBACK_CHUNK_GAP = 4.0

_TIMING_RE = re.compile(r"^\s*(?P<start>[\d:.,]+)\s*-->\s*(?P<end>[\d:.,]+)")
_VOICE_RE = re.compile(
    r"^(?P<open><v(?:\.[^\s>]+)?\s+(?P<speaker>[^>]*)>)(?P<text>.*?)(?P<close></v>)?$", re.S
)
_COLON_RE = re.compile(r"^(?P<name>[^:\n]{1,100}):(?P<sep>[ \t]+)(?P<text>\S.*)$", re.S)
_CUE_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

#: Speakers assigned by hand, recorded in the transcript itself as a WebVTT
#: NOTE. Detection is a guess and has to stay conservative; an assignment is a
#: decision and must survive re-parsing whatever it looks like. Keeping it in
#: the file rather than a sidecar means one source of truth, readable by
#: anything that opens the transcript, and ignored by anything that plays it.
_ROSTER_RE = re.compile(r"^NOTE\s+speakers:\s*(.+)$", re.IGNORECASE)


#: Keys offered to speakers that do not name one. Digits, because they already
#: mean "pick item N" and nothing else in the reader uses them -- and 1-5 fall
#: under the left hand, which is the one not on the trackpad.
DEFAULT_KEYS = "123456789"


def read_speakers(content: str) -> list[dict]:
    """The roster, as ``{"key": "1", "name": "Ada Lovelace"}`` entries.

    Written as ``NOTE speakers: 1=Ada Lovelace, 2=Participant``. A bare name is
    allowed and gets the next free key, so a roster typed by hand does not have
    to bother with the left-hand column.
    """
    for raw in content.split("\n"):
        match = _ROSTER_RE.match(raw.strip())
        if not match:
            continue
        entries: list[dict] = []
        for item in match.group(1).split(","):
            item = item.strip()
            if not item:
                continue
            key, sep, name = item.partition("=")
            # Only a single character counts as a key; anything longer is a name
            # that happens to contain an equals sign.
            if sep and len(key.strip()) == 1 and name.strip():
                entries.append({"key": key.strip(), "name": name.strip()})
            else:
                entries.append({"key": None, "name": item})
        return assign_keys(entries)
    return []


def assign_keys(entries: list[dict]) -> list[dict]:
    """Give every speaker a key, leaving the ones already chosen alone."""
    taken = {e["key"] for e in entries if e.get("key")}
    spare = [k for k in DEFAULT_KEYS if k not in taken]
    out = []
    for entry in entries:
        key = entry.get("key") or (spare.pop(0) if spare else None)
        out.append({"key": key, "name": entry["name"]})
    return out


def read_roster(content: str) -> list[str]:
    """Just the names, for the parser -- an assignment is never a guess."""
    return [entry["name"] for entry in read_speakers(content)]


def write_roster(content: str, entries: list[dict]) -> str:
    """Put the roster back, replacing any previous one, just under the header."""
    rendered = ", ".join(
        f"{e['key']}={e['name']}" if e.get("key") else e["name"] for e in assign_keys(entries)
    )
    line = f"NOTE speakers: {rendered}"
    lines = content.split("\n")
    for index, raw in enumerate(lines):
        if _ROSTER_RE.match(raw.strip()):
            lines[index] = line
            return "\n".join(lines)

    # No roster yet: sit it after the WEBVTT header and its blank line.
    for index, raw in enumerate(lines):
        if raw.strip().upper().startswith("WEBVTT"):
            return "\n".join(lines[: index + 1] + ["", line] + lines[index + 1 :])
    return "\n".join([line, ""] + lines)

#: Leading characters to look past when asking whether a word is capitalized.
_WORD_LEAD = "(\"'[{"


def looks_like_a_name(prefix: str) -> bool:
    """Whether a prefix is name-shaped enough to accept on a single sighting.

    Two or more capitalized words, starting with one. Requiring the *second*
    word to begin with a letter would reject real Zoom display names that carry
    a bracketed nickname or an affiliation, as in "B.F. (Jim) Lightning, Brown
    U. USA" -- so capitalization is counted across the whole prefix instead.

    "So here's my point" still fails: only one word in it is capitalized.
    """
    words = prefix.split()
    if len(words) < 2 or not prefix[:1].isupper():
        return False
    capitalized = sum(1 for word in words if word.lstrip(_WORD_LEAD)[:1].isupper())
    return capitalized >= 2


# Prefixes starting with one of these are sentence fragments, not names.
_FUNCTION_WORDS = {
    "a", "actually", "an", "and", "anyway", "as", "at", "basically", "because",
    "but", "either", "for", "he", "her", "here", "his", "honestly", "i", "if",
    "in", "is", "it", "its", "just", "like", "look", "maybe", "my", "no", "now",
    "of", "ok", "okay", "on", "one", "or", "our", "really", "right", "she", "so",
    "that", "the", "their", "then", "there", "they", "this", "to", "we", "well",
    "what", "when", "where", "which", "while", "who", "why", "yeah", "yes", "you",
    "your",
}


class VTTParseError(ValueError):
    """Raised when a file does not look like WebVTT at all."""


def parse_timestamp(raw: str) -> float:
    """Parse ``HH:MM:SS.mmm`` or ``MM:SS.mmm`` into seconds."""
    text = raw.strip().replace(",", ".")
    parts = text.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, minutes, seconds = "0", parts[0], parts[1]
    else:
        raise VTTParseError(f"unrecognized timestamp: {raw!r}")
    try:
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError as exc:
        raise VTTParseError(f"unrecognized timestamp: {raw!r}") from exc


def format_timestamp(seconds: float) -> str:
    """Render seconds as ``H:MM:SS`` (or ``M:SS`` under an hour)."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _clean(text: str) -> str:
    """Strip inline cue tags and collapse the whitespace from wrapped payloads."""
    return re.sub(r"\s+", " ", _CUE_TAG_RE.sub("", text)).strip()


@dataclass
class _Line:
    start: int
    end: int
    text: str


def _iter_lines(content: str) -> list[_Line]:
    """Split into lines, keeping each line's offsets in the original string.

    Offsets point into the file exactly as read, so a splice can be applied
    without normalizing line endings or disturbing anything else.
    """
    lines: list[_Line] = []
    offset = 0
    for raw in content.split("\n"):
        text = raw[:-1] if raw.endswith("\r") else raw
        lines.append(_Line(offset, offset + len(text), text))
        offset += len(raw) + 1
    return lines


def _iter_blocks(lines: list[_Line]) -> list[list[_Line]]:
    """Group lines into cue blocks, dropping headers and metadata blocks."""
    blocks: list[list[_Line]] = []
    current: list[_Line] = []
    for line in lines:
        if line.text.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    kept = []
    for block in blocks:
        head = block[0].text.strip().upper()
        if head.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        kept.append(block)
    return kept


#: A Zoom display name is not just a name: people join as "Firstname M.
#: (Nickname) Lastname, Department, University, Country". Capping the prefix
#: tightly would reject those outright, and a rejected prefix does not fail
#: loudly -- the cue quietly inherits whoever spoke before, so two people merge
#: into one. What keeps a sentence from being mistaken for a speaker is the
#: function-word and capitalization tests, not length.
MAX_PREFIX_CHARS = 100
MAX_PREFIX_WORDS = 12

#: Above this many words, a prefix has to recur before it counts as a speaker.
#: A long display name repeats every time that person talks; a long sentence
#: fragment that happens to precede a colon does not.
SIGHT_ONCE_MAX_WORDS = 6


def starts_with_function_word(prefix: str) -> bool:
    """Whether a prefix opens with a word that marks it as a sentence fragment."""
    words = prefix.split()
    return bool(words) and words[0].strip(",.'\"").lower() in _FUNCTION_WORDS


def is_plausible_speaker_prefix(prefix: str) -> bool:
    """Loose shape test for a ``Name:`` prefix, before frequency is considered."""
    prefix = prefix.strip()
    if not 1 <= len(prefix) <= MAX_PREFIX_CHARS:
        return False
    words = prefix.split()
    if not words or len(words) > MAX_PREFIX_WORDS:
        return False
    if any(ch in prefix for ch in "?!"):
        return False
    # A trailing comma is normal on a display name whose affiliation got cut off;
    # only the endings that read as mid-sentence are rejected.
    if prefix[-1] in ";-":
        return False
    if starts_with_function_word(prefix):
        return False
    return True


def _accept_speakers(counts: dict[str, int], roster: set[str] | None = None) -> set[str]:
    """Decide which candidate prefixes are real speakers, given the whole file.

    A prefix qualifies if it is plausibly name-shaped *and* either reads as a
    proper name or appears more than once. Requiring recurrence for anything but
    a clean two-word name is what keeps a stray mid-sentence colon from inventing
    a speaker: an accidental prefix would have to appear verbatim twice.
    """
    roster = {name.lower() for name in roster or ()}
    # Spellings that differ only in case are one speaker, counted together.
    totals: dict[str, int] = {}
    for prefix, count in counts.items():
        totals[prefix.lower()] = totals.get(prefix.lower(), 0) + count
    accepted = set()
    for prefix in counts:
        count = totals[prefix.lower()]
        # An assigned name is not a guess, so the heuristics do not get a vote:
        # "Interviewer" on a single line would otherwise fail every test below.
        if prefix.lower() in roster:
            accepted.add(prefix)
            continue
        if not is_plausible_speaker_prefix(prefix):
            continue
        if count >= 2 or (
            looks_like_a_name(prefix) and len(prefix.split()) <= SIGHT_ONCE_MAX_WORDS
        ):
            accepted.add(prefix)
    return accepted


@dataclass
class _Resolved:
    speaker: str | None
    text: str
    prefix: str
    suffix: str


def _extract_payload_speakers(
    payloads: list[str], roster: set[str] | None = None
) -> tuple[list[_Resolved], str]:
    """Resolve each payload into speaker, readable text, and what was stripped."""
    # Voice tags are unambiguous -- if the file uses them, trust them exclusively
    # and never run the colon heuristic.
    voice_hits = [_VOICE_RE.match(p) for p in payloads]
    if any(voice_hits):
        resolved = []
        for payload, hit in zip(payloads, voice_hits):
            if hit:
                resolved.append(
                    _Resolved(
                        speaker=_clean(hit.group("speaker")) or None,
                        text=_clean(hit.group("text")),
                        prefix=hit.group("open"),
                        suffix=hit.group("close") or "",
                    )
                )
            else:
                resolved.append(_Resolved(None, _clean(payload), "", ""))
        return resolved, "voice-tag"

    counts: dict[str, int] = {}
    matches = [_COLON_RE.match(p) for p in payloads]
    for hit in matches:
        if hit:
            name = hit.group("name").strip()
            counts[name] = counts.get(name, 0) + 1

    accepted = _accept_speakers(counts, roster)
    if not accepted:
        return [_Resolved(None, _clean(p), "", "") for p in payloads], "none"

    resolved = []
    for payload, hit in zip(payloads, matches):
        if hit and hit.group("name").strip() in accepted:
            resolved.append(
                _Resolved(
                    speaker=hit.group("name").strip(),
                    text=_clean(hit.group("text")),
                    prefix=hit.group("name") + ":" + hit.group("sep"),
                    suffix="",
                )
            )
        else:
            # A rejected prefix means the colon was punctuation: keep the whole
            # line and let the cue inherit the speaker still holding the floor.
            resolved.append(_Resolved(None, _clean(payload), "", ""))
    return resolved, "colon-prefix"


def parse_cues(content: str) -> tuple[list[Cue], str]:
    """Parse VTT text into cues. Returns the cues and the detection method."""
    timings: list[tuple[float, float]] = []
    payloads: list[str] = []
    spans: list[tuple[int, int, int]] = []

    for block in _iter_blocks(_iter_lines(content)):
        timing_idx = next((i for i, line in enumerate(block) if "-->" in line.text), None)
        if timing_idx is None:
            continue  # a block with no timing line is not a cue
        hit = _TIMING_RE.match(block[timing_idx].text)
        if not hit:
            continue
        try:
            start = parse_timestamp(hit.group("start"))
            end = parse_timestamp(hit.group("end"))
        except VTTParseError:
            continue
        payload_lines = block[timing_idx + 1 :]
        if not payload_lines:
            continue
        payload = " ".join(line.text for line in payload_lines).strip()
        if not payload:
            continue
        timings.append((start, max(start, end)))
        payloads.append(payload)
        spans.append((payload_lines[0].start, payload_lines[-1].end, block[timing_idx].start))

    if not timings:
        raise VTTParseError("no cues found; is this a WebVTT file?")

    resolved, method = _extract_payload_speakers(payloads, set(read_roster(content)))

    cues: list[Cue] = []
    current_speaker: str | None = None
    for index, ((start, end), item, span) in enumerate(zip(timings, resolved, spans)):
        if item.speaker:
            current_speaker = item.speaker
        if not item.text:
            continue
        cues.append(
            Cue(
                id=f"c{index}",
                index=index,
                start=start,
                end=end,
                speaker=current_speaker,
                text=item.text,
                source_start=span[0],
                source_end=span[1],
                timing_start=span[2],
                prefix=item.prefix,
                suffix=item.suffix,
            )
        )
    return cues, method


def unify_speakers(cues: list[Cue], roster: list[str]) -> list[Cue]:
    """Give every spelling of a speaker that differs only in case one name.

    The name is the roster's spelling when the speaker is on it, otherwise the
    first spelling in the transcript. The file itself is left as it is.
    """
    names = {name.lower(): name for name in reversed(roster)}
    for cue in cues:
        if cue.speaker:
            names.setdefault(cue.speaker.lower(), cue.speaker)
    return [replace(c, speaker=names[c.speaker.lower()]) if c.speaker else c for c in cues]


def build_chunks(
    cues: list[Cue], speaker_count: int, assigned: set[str] | None = None
) -> list[Chunk]:
    """Group cues into display blocks.

    With two or more speakers a chunk is one contiguous run from one of them:
    long turns stay whole and get paragraph breaks instead.

    Exactly one speaker means the label carries no information, and usually
    means the room was recorded through a single microphone -- everybody present
    is filed under whoever started the meeting. Joining on that label would
    invent a monologue out of a conversation, so every caption stands alone
    until speakers are actually assigned. From two speakers on, joining resumes.

    No speaker at all is a different situation -- a transcript that never had
    labels -- so those still break on long pauses, which at least reads.

    Once anyone has been assigned by hand, joining follows the assignment:
    contiguous cues merge only when their speaker is on the roster. Whatever
    label the transcript arrived with stays line by line, so a labelling pass can
    work through it one caption at a time instead of watching the unlabelled
    remainder collapse into a single block after the first assignment.
    """
    assigned = assigned or set()
    chunks: list[Chunk] = []
    run: list[Cue] = []

    def flush() -> None:
        if not run:
            return
        paragraphs: list[list[str]] = [[]]
        previous: Cue | None = None
        for cue in run:
            if previous is not None and cue.start - previous.end > PARAGRAPH_GAP:
                paragraphs.append([])
            paragraphs[-1].append(cue.id)
            previous = cue
        chunks.append(
            Chunk(
                id=f"k{len(chunks)}",
                index=len(chunks),
                speaker=run[0].speaker,
                start=run[0].start,
                end=run[-1].end,
                cue_ids=[c.id for c in run],
                paragraphs=[p for p in paragraphs if p],
            )
        )
        run.clear()

    for cue in cues:
        if run:
            # A new recording always starts a new block, even mid-sentence from
            # the same speaker -- the interruption is real and worth seeing.
            if cue.part_index != run[-1].part_index:
                boundary = True
            elif speaker_count == 1:
                boundary = True
            elif speaker_count >= 2:
                same = cue.speaker == run[-1].speaker
                # With a roster in play, only assigned speakers join up.
                boundary = not same or (bool(assigned) and cue.speaker not in assigned)
            else:
                boundary = cue.start - run[-1].end > FALLBACK_CHUNK_GAP
            if boundary:
                flush()
        run.append(cue)
    flush()

    by_id = {cue.id: cue for cue in cues}
    seen_parts: set[int] = set()
    for chunk in chunks:
        first = by_id.get(chunk.cue_ids[0])
        chunk.part_index = first.part_index if first else 0
        chunk.starts_part = chunk.part_index not in seen_parts
        seen_parts.add(chunk.part_index)
    return chunks


def parse_vtt(content: str, source_name: str = "transcript.vtt") -> Transcript:
    cues, method = parse_cues(content)
    roster = read_speakers(content)
    cues = unify_speakers(cues, [e["name"] for e in roster])
    speakers: list[str] = []
    for cue in cues:
        if cue.speaker and cue.speaker not in speakers:
            speakers.append(cue.speaker)
    chunks = build_chunks(cues, len(speakers), {e['name'] for e in roster})
    digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
    return Transcript(
        cues=cues,
        chunks=chunks,
        speakers=speakers,
        source_name=source_name,
        sha256=digest,
        speaker_detection=method,
        roster=roster,
    )


def assemble_session(specs: list[dict], parts: list[Part], source_name: str) -> Transcript:
    """Lay several parsed recordings end to end on one session timeline.

    Each spec carries that part's locally-timed cues and the offset at which the
    part begins. Cues are renumbered globally in time order, so ids stay stable
    when a later part is added to a folder -- which matters because saved quotes
    are anchored to cue ids.
    """
    combined: list[Cue] = []
    for spec in specs:
        offset = spec["offset"]
        part_index = spec["index"]
        for cue in spec["cues"]:
            combined.append(cue.shifted(f"c{len(combined)}", len(combined), offset, part_index))

    roster = assign_keys(
        list({e['name'].lower(): e for spec in specs for e in spec.get('roster', [])}.values())
    )
    combined = unify_speakers(combined, [e["name"] for e in roster])
    speakers: list[str] = []
    for cue in combined:
        if cue.speaker and cue.speaker not in speakers:
            speakers.append(cue.speaker)

    chunks = build_chunks(combined, len(speakers), {e['name'] for e in roster})
    digest = session_digest([spec["sha256"] for spec in specs])
    methods = {spec["method"] for spec in specs}
    method = methods.pop() if len(methods) == 1 else "mixed"

    return Transcript(
        cues=combined,
        chunks=chunks,
        speakers=speakers,
        source_name=source_name,
        sha256=digest,
        speaker_detection=method,
        parts=parts,
        roster=roster,
    )


def session_digest(part_digests: list[str]) -> str:
    """The session's fingerprint, recomputed whenever a transcript is edited.

    A one-part session keeps the plain content digest, so folders that predate
    multi-part support do not read as "the transcript changed" on first open.
    """
    if len(part_digests) == 1:
        return part_digests[0]
    return hashlib.sha256(" ".join(part_digests).encode("utf-8")).hexdigest()


def vtt_timestamp(seconds: float) -> str:
    """``HH:MM:SS.mmm``, the form a cue's timing line wants."""
    seconds = max(0.0, seconds)
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:  # rounding carried
        millis, secs = 0, secs + 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def split_cue_block(
    content: str, cue: Cue, offset: int, at: float, base: float = 0.0, tail_at: float | None = None
) -> str:
    """Turn one cue into two, cut at a point inside its text.

    Zoom regularly puts two people in one caption -- an answer beginning halfway
    through the interviewer's line -- and no amount of reassigning whole cues can
    separate them. This rewrites the block as two, sharing the boundary time, so
    each half can be attributed on its own.

    The whole block is replaced rather than patched, because the first half's end
    time changes as well as its words. The second half is written without a cue
    identifier: they are optional in WebVTT and carry no meaning, and renumbering
    every later cue would turn a two-line edit into a whole-file rewrite.

    ``base`` is where this part sits on the session timeline. Cue times are
    session times, but a file's own timestamps start from zero, so the offset has
    to come back off before anything is written -- otherwise a split in a later
    part of an interrupted session would write times minutes ahead of the audio.

    ``tail_at`` lets the halves *not* meet. Where the words have been aligned to
    the audio, the first half can end on its last word and the second begin on its
    first, leaving the pause between two speakers belonging to neither -- which is
    both more accurate and what a reader listening back expects. Without a
    measurement the two share one interpolated boundary, as Zoom's own do.
    """
    head = _clean(cue.text[:offset])
    tail = _clean(cue.text[offset:])
    head_end = vtt_timestamp(at - base)
    tail_start = vtt_timestamp((at if tail_at is None else tail_at) - base)
    replacement = (
        f"{vtt_timestamp(cue.start - base)} --> {head_end}\n{cue.prefix}{head}{cue.suffix}\n\n"
        f"{tail_start} --> {vtt_timestamp(cue.end - base)}\n{cue.prefix}{tail}{cue.suffix}"
    )
    return content[: cue.timing_start] + replacement + content[cue.source_end :]


def merged_payload(cues: list[Cue]) -> tuple[str, list[int], list[int]]:
    """The text a merge produces, and where each cue's words land inside it.

    Returns the joined text, the offset at which each cue's contribution begins,
    and how long each contribution is. The offsets are what re-anchor a quote that
    was pointing inside one of the captions being absorbed, and they come from here
    rather than being recomputed by the caller so the two can never disagree about
    where a word ended up.
    """
    merged = ""
    starts: list[int] = []
    lengths: list[int] = []
    for cue in cues:
        piece = _clean(cue.text)
        if merged and piece:
            merged += " "
        starts.append(len(merged))
        lengths.append(len(piece))
        merged += piece
    return merged, starts, lengths


def merge_cue_blocks(content: str, cues: list[Cue], base: float = 0.0) -> str:
    """Turn a run of consecutive cues into one, spanning from the first to the last.

    The counterpart to :func:`split_cue_block`, and the fix for Zoom's other
    segmentation failure: one sentence chopped across three captions, so a quote
    that reads as a single thought is three separate anchors underneath.

    Everything from the first cue's timing line to the last one's payload is
    replaced, which takes the intervening cue identifiers and timing lines with
    it. The surviving block keeps the first cue's identifier -- it sits before the
    timing line, outside the replaced span -- so the file is not renumbered.

    The words are joined in order with single spaces, so the *sequence* of words in
    the file is unchanged. That is what lets measured word timings survive a merge
    without being remapped, exactly as they survive a split.
    """
    if not cues:
        return content
    first, last = cues[0], cues[-1]
    text, _, _ = merged_payload(cues)
    replacement = (
        f"{vtt_timestamp(first.start - base)} --> {vtt_timestamp(last.end - base)}\n"
        f"{first.prefix}{text}{first.suffix}"
    )
    return content[: first.timing_start] + replacement + content[last.source_end :]


def splice_payload(content: str, cue: Cue, prefix: str, text: str, suffix: str) -> str:
    """Rewrite one cue's payload, leaving every other byte as it was."""
    return content[: cue.source_start] + f"{prefix}{text}{suffix}" + content[cue.source_end :]


def splice_cue(content: str, cue: Cue, new_text: str) -> str:
    """Replace one cue's words, keeping whoever it was attributed to."""
    return splice_payload(content, cue, cue.prefix, new_text, cue.suffix)


def speaker_prefix(speaker: str, style: str) -> tuple[str, str]:
    """How this file writes a speaker, so an edit matches what is already there."""
    if style == "voice-tag":
        return f"<v {speaker}>", ""
    return f"{speaker}: ", ""


def splice_speaker(content: str, cue: Cue, speaker: str, style: str) -> str:
    """Reattribute one cue, adding a label to a line that never had one."""
    prefix, suffix = speaker_prefix(speaker, style)
    return splice_payload(content, cue, prefix, cue.text, suffix)


def load_vtt(path: Path) -> Transcript:
    # Zoom occasionally emits stray bytes in otherwise-UTF-8 files; replacing is
    # better than refusing to open an hour of transcript over one bad character.
    content = path.read_text(encoding="utf-8", errors="replace")
    return parse_vtt(content, source_name=path.name)
