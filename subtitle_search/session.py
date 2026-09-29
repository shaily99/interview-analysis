"""Recording folder discovery and the in-memory registry.

Everything in a folder is one session. Zoom splits a meeting into several
recordings when it gets interrupted -- someone drops, the host reconnects -- and
each part restarts its transcript at 00:00. Discovery groups the folder's files
into parts, orders them, and lays them end to end on a single timeline so times
run continuously across the whole session.

The registry holds the study's recordings, addressed by a stable id, plus its
coders, each coder's codebooks and themes, and the common codebooks and themes.
``refresh()`` re-reads all of them from disk.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .editing import BACKUP_SUFFIX, backup_path, read_source
from .codebook import TextCodebook
from .coders import CODERS_DIRNAME, CoderDirectory
from .common import COMMON, CommonCodebook, CommonQuotes, CommonSet, CommonSpans
from .highlights import QUOTES_FILENAME, HighlightStore, SessionQuotes
from .library import CommonThemeStore, ThemeStore
from .mediainfo import container_duration
from .models import Part, Transcript
from .timings import WORDS_FILENAME, TimingStore, attach, word_index_map
from .timings import coverage as timing_coverage
from .video_codes import CODEBOOK_FILENAME as VIDEO_CODEBOOK_FILENAME
from .video_codes import LEGACY_CODEBOOK_FILENAME, LEGACY_SPANS_FILENAME, SessionVideoCodes, VideoCodebook, VideoCodeStore
from .video_codes import SPANS_FILENAME as VIDEO_CODES_FILENAME
from .vtt import VTTParseError, assemble_session, parse_cues, read_speakers, session_digest

#: Searched in order -- video first, since the collapsible pane can show it and
#: an audio-only fallback is a strictly smaller feature.
VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mov", ".webm", ".mkv")
AUDIO_EXTENSIONS = (".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg")
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS + AUDIO_EXTENSIONS

#: Zoom cloud downloads: GMT20240101-140523_Recording.transcript.vtt
_GMT_RE = re.compile(r"GMT(\d{8})-(\d{6})")
#: A trailing counter, as in zoom_0.mp4 / zoom_1.mp4.
_COUNTER_RE = re.compile(r"(\d+)(?!.*\d)")
#: Decoration Zoom hangs off the shared stem, stripped before pairing by name.
_STEM_NOISE_RE = re.compile(
    r"(\.transcript|\.cc|_recording|_\d{3,4}x\d{3,4}|audio_only|_audio|_video)",
    re.IGNORECASE,
)

#: Below this, a gap between parts is recording overhead rather than a real
#: interruption worth interrupting the reader for.
MIN_REPORTABLE_GAP = 2.0

#: Used only when a part's media is missing or unreadable, so the next part does
#: not start on top of the previous one's last word.
FALLBACK_TAIL = 1.0


class RecordingError(ValueError):
    """Raised when a folder cannot be opened as a recording."""


def _recording_id(folder: Path) -> str:
    return hashlib.sha1(str(folder.resolve()).encode("utf-8")).hexdigest()[:12]


def _timestamp_key(path: Path) -> tuple[str, datetime] | None:
    match = _GMT_RE.search(path.name)
    if not match:
        return None
    try:
        stamp = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return match.group(0), stamp


def _normalized_stem(path: Path) -> str:
    stem = path.name
    for suffix in (".vtt", ".transcript.vtt"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    stem = Path(stem).stem if "." in stem else stem
    return _STEM_NOISE_RE.sub("", stem).strip(" _-.").lower()


def _sort_index(path: Path) -> tuple:
    """Order files when no timestamp is available: counter first, then mtime."""
    match = _COUNTER_RE.search(path.stem)
    counter = int(match.group(1)) if match else -1
    try:
        modified = path.stat().st_mtime
    except OSError:
        modified = 0.0
    return (counter, modified, path.name.lower())


def _best_media(candidates: list[Path]) -> Path | None:
    by_extension: dict[str, list[Path]] = {}
    for path in candidates:
        by_extension.setdefault(path.suffix.lower(), []).append(path)
    for extension in MEDIA_EXTENSIONS:
        if extension in by_extension:
            return max(by_extension[extension], key=lambda p: p.stat().st_size)
    return None


@dataclass
class PartFiles:
    vtt_path: Path
    media_path: Path | None
    started_at: datetime | None


def discover_parts(folder: Path) -> list[PartFiles]:
    """Group a folder's files into ordered session parts.

    Pairing is attempted three ways, most reliable first: the GMT timestamp Zoom
    stamps into cloud filenames, then a shared stem once Zoom's decoration is
    stripped, then plain ordering. Every path ends with transcripts in a defined
    order, because a wrong order silently corrupts every timestamp downstream.
    """
    # Backups made before the first correction sit in the same folder with the
    # same extension. Without this they would be read as another recording, and
    # every edit would silently double the session.
    vtts = sorted(
        p for p in folder.glob("*.vtt") if p.is_file() and not p.stem.endswith(BACKUP_SUFFIX)
    )
    if not vtts:
        raise RecordingError(f"no .vtt file found in {folder}")

    media = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS]

    # 1. Zoom cloud naming: group everything sharing a GMT timestamp token.
    keyed_vtts = [(v, _timestamp_key(v)) for v in vtts]
    if all(key is not None for _, key in keyed_vtts):
        parts: list[PartFiles] = []
        for vtt, key in sorted(keyed_vtts, key=lambda item: item[1][1]):
            token, stamp = key
            matching = [m for m in media if token in m.name]
            parts.append(PartFiles(vtt, _best_media(matching), stamp))
        return parts

    # 2. One transcript: the whole folder's media belongs to it.
    if len(vtts) == 1:
        return [PartFiles(vtts[0], _best_media(media), None)]

    # 3. Pair on a shared stem once Zoom's decoration is removed.
    by_stem: dict[str, list[Path]] = {}
    for path in media:
        by_stem.setdefault(_normalized_stem(path), []).append(path)
    ordered = sorted(vtts, key=_sort_index)
    if all(_normalized_stem(v) in by_stem for v in ordered):
        return [PartFiles(v, _best_media(by_stem[_normalized_stem(v)]), None) for v in ordered]

    # 4. Nothing lines up: order both sides and zip, leaving extras unpaired.
    ordered_media = sorted(media, key=_sort_index)
    return [
        PartFiles(vtt, ordered_media[i] if i < len(ordered_media) else None, None)
        for i, vtt in enumerate(ordered)
    ]


#: The shared quotes file from before coders existed. Read by nothing; only reported.
LEGACY_QUOTES_FILENAME = "session.highlights.json"

TEXT_CODEBOOK_FILENAME = "text_codebook.json"

#: One coder's themes, beside their codebooks; and the shared file from before coders.
THEMES_FILENAME = "themes.json"
LEGACY_THEMES_FILENAME = "library.themes.json"

#: Items returned from common to a coder, waiting for that coder to claim them.
RETURNS_FILENAME = "returns.json"

#: Who put which common code on a common quote, and who contributed to a common span.
COMMON_QUOTE_CODES_FILENAME = "quote_codes.json"
COMMON_SPAN_PARTS_FILENAME = "video_code_parts.json"


class CoderBooks:
    """Every coder's two codebooks, from ``<study root>/coders/<id>/``.

    Opened on first use and kept, so a quote store and the routes share one
    in-memory codebook per coder. ``reload`` drops them all, so the next use reads
    whatever a collaborator's sync has written since.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._text: dict[str, TextCodebook] = {}
        self._video: dict[str, VideoCodebook] = {}
        #: The agreed codes, answered for the coder label "common".
        self.common_text = CommonCodebook(self.root, TEXT_CODEBOOK_FILENAME)
        self.common_video = CommonCodebook(self.root, VIDEO_CODEBOOK_FILENAME)

    def _folder(self, coder: str) -> Path:
        return self.root / CODERS_DIRNAME / coder

    def text(self, coder: str) -> TextCodebook:
        if coder == COMMON:
            return self.common_text
        if coder not in self._text:
            self._text[coder] = TextCodebook(self._folder(coder) / TEXT_CODEBOOK_FILENAME)
        return self._text[coder]

    def video(self, coder: str) -> VideoCodebook:
        if coder == COMMON:
            return self.common_video
        if coder not in self._video:
            self._video[coder] = VideoCodebook(self._folder(coder) / VIDEO_CODEBOOK_FILENAME)
        return self._video[coder]

    def coders(self) -> list[str]:
        folder = self.root / CODERS_DIRNAME
        return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []

    def all_text(self) -> list[tuple[str, dict]]:
        """Every text code: the common ones first, then each coder's."""
        return [(COMMON, code) for code in self.common_text.list()] + [
            (c, code) for c in self.coders() for code in self.text(c).list()
        ]

    def all_video(self) -> list[tuple[str, dict]]:
        return [(COMMON, code) for code in self.common_video.list()] + [
            (c, code) for c in self.coders() for code in self.video(c).list()
        ]

    def reload(self) -> None:
        self._text.clear()
        self._video.clear()
        self.common_text.reload()
        self.common_video.reload()


def build_transcript(folder: Path, part_files: list[PartFiles]) -> tuple[Transcript, list[str]]:
    """Parse each part and lay them end to end on one continuous timeline.

    Returns the transcript and each part's raw source text, which is kept in
    memory so a correction can be spliced into it without re-reading from disk.
    """
    specs: list[dict] = []
    parts: list[Part] = []
    sources: list[str] = []
    offset = 0.0
    previous_end_wall: datetime | None = None

    for index, files in enumerate(part_files):
        content = read_source(files.vtt_path)
        sources.append(content)
        cues, method = parse_cues(content)

        # Part N+1 starts where part N's *recording* ended, not where its last
        # caption ended -- recordings usually run on past the final word, and
        # using the caption would pull every later part earlier.
        measured = container_duration(files.media_path) if files.media_path else None
        last_cue_end = cues[-1].end if cues else 0.0
        duration = measured if measured and measured >= last_cue_end else last_cue_end + FALLBACK_TAIL

        gap_before: float | None = None
        if files.started_at and previous_end_wall:
            gap_before = max(0.0, (files.started_at - previous_end_wall).total_seconds())

        digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        parts.append(
            Part(
                index=index,
                vtt_name=files.vtt_path.name,
                media_name=files.media_path.name if files.media_path else None,
                media_kind=_media_kind(files.media_path),
                offset=offset,
                duration=duration,
                started_at=files.started_at,
                gap_before=gap_before if (gap_before or 0) >= MIN_REPORTABLE_GAP else None,
                sha256=digest,
                edited=backup_path(files.vtt_path).exists(),
            )
        )
        specs.append(
            {
                "cues": cues,
                "offset": offset,
                "index": index,
                "method": method,
                "sha256": digest,
                "roster": read_speakers(content),
            }
        )

        offset += duration
        if files.started_at:
            previous_end_wall = files.started_at + timedelta(seconds=duration)

    name = part_files[0].vtt_path.name if len(part_files) == 1 else f"{len(part_files)} recordings"
    transcript = assemble_session(specs, parts, source_name=name)
    apply_timings(folder, transcript)
    return transcript, sources


def apply_timings(
    folder: Path, transcript: Transcript, store: TimingStore | None = None
) -> TimingStore:
    """Hand every aligned cue its measured word spans.

    A folder with no timings file is left exactly as it was, which is what makes
    alignment opt-in: nothing about the reader changes until it is run.
    """
    store = store if store is not None else TimingStore(folder / WORDS_FILENAME)
    if store.empty:
        return store
    for part in transcript.parts:
        measured = store.words(part.vtt_name)
        if not measured:
            continue
        for cue, spans in attach(transcript.cues_in_part(part.index), measured, part.offset):
            if spans:
                transcript.replace_cue(cue.with_words(spans))
    return store


def _media_kind(path: Path | None) -> str | None:
    if path is None:
        return None
    return "video" if path.suffix.lower() in VIDEO_EXTENSIONS else "audio"


@dataclass
class Recording:
    id: str
    folder: Path
    transcript: Transcript
    #: Every coder's quotes; ``store`` so editing code that re-anchors quotes
    #: after a correction reaches all of them without knowing about coders.
    store: SessionQuotes
    part_files: list[PartFiles]
    #: Each part's transcript file as text, kept so corrections can be spliced
    #: into it without re-reading and without reformatting the rest.
    sources: list[str]
    #: Measured word timings, empty until alignment has been run.
    timings: TimingStore
    #: Every coder's coded spans of the video, separate from quotes.
    video_codes: SessionVideoCodes
    #: Where each coder's text and video codebooks come from.
    books: CoderBooks
    #: Shared files from before coders, left alone and only reported.
    legacy_files: list[str]

    def _coder_folder(self, coder: str) -> Path:
        return self.folder / CODERS_DIRNAME / coder

    def load_coders(self) -> None:
        """Open one quote store and one span store per coder folder on disk."""
        coders_dir = self.folder / CODERS_DIRNAME
        found = sorted(p.name for p in coders_dir.iterdir() if p.is_dir()) if coders_dir.is_dir() else []
        quote_stores = {c: self._quotes_for(c) for c in found if (self._coder_folder(c) / QUOTES_FILENAME).is_file()}
        span_stores = {c: self._spans_for(c) for c in found if (self._coder_folder(c) / VIDEO_CODES_FILENAME).is_file()}
        self.common_quotes = CommonQuotes(self.folder, QUOTES_FILENAME, COMMON_QUOTE_CODES_FILENAME)
        self.common_spans = CommonSpans(self.folder, VIDEO_CODES_FILENAME, COMMON_SPAN_PARTS_FILENAME)
        self.returns = CommonSet(self.folder, RETURNS_FILENAME)
        self.store = SessionQuotes(quote_stores, factory=self._quotes_for, common=self.common_quotes, returns=self.returns)
        self.store.transcript = self.transcript
        self.video_codes = SessionVideoCodes(span_stores, factory=self._spans_for, common=self.common_spans)

    def _quotes_for(self, coder: str) -> HighlightStore:
        return HighlightStore(self._coder_folder(coder) / QUOTES_FILENAME, self.transcript, self.books.text(coder))

    def _spans_for(self, coder: str) -> VideoCodeStore:
        return VideoCodeStore(self._coder_folder(coder) / VIDEO_CODES_FILENAME)

    def refresh(self) -> None:
        """Re-read every coder's files and the transcript, for work synced in since opening.

        Coders first: they depend only on the folder, so a transcript that is
        mid-sync and cannot be parsed still leaves the quotes and spans current,
        on the transcript already in hand. Its error is then raised to the caller.
        """
        self.load_coders()
        transcript, sources = build_transcript(self.folder, self.part_files)
        self.transcript = transcript
        self.sources = sources
        self.timings = TimingStore(self.folder / WORDS_FILENAME)
        self.store.transcript = transcript

    def reload_transcript(self) -> None:
        """Re-derive the session after its transcript changed on disk.

        Reassigning a speaker changes how cues group, so chunks have to be
        rebuilt rather than patched. Cue ids are positional, so an edit that
        changes the cue *count* -- splitting one caption in two -- moves the ids
        of everything after it, and the caller is responsible for re-anchoring
        saved quotes across that shift.
        """
        transcript, sources = build_transcript(self.folder, self.part_files)
        self.transcript = transcript
        self.sources = sources
        self.timings = TimingStore(self.folder / WORDS_FILENAME)
        self.store.transcript = transcript
        self.store.restamp()

    def measure(self, cues) -> int:
        """Align captions to their audio and keep the word timings that come back.

        Returns how many words were measured. Captions from several parts can be
        passed at once; each is aligned against its own recording, since a
        session's parts are separate files on one timeline.
        """
        from .alignment import align_cues  # deferred: pulls in torch on first use

        grouped: dict[int, list] = defaultdict(list)
        for cue in cues:
            grouped[cue.part_index].append(cue)

        written = 0
        for part_index, group in sorted(grouped.items()):
            files = self.part_files[part_index]
            if files.media_path is None:
                continue  # nothing to align against
            part = self.transcript.part(part_index)
            positions = word_index_map(self.transcript.cues_in_part(part_index))
            targets = [
                (cue, positions.get(cue.id, 0))
                for cue in sorted(group, key=lambda c: c.index)
            ]
            measured = align_cues(files.media_path, targets, part.offset if part else 0.0)
            if measured:
                self.timings.record(files.vtt_path.name, measured)
                written += len(measured)

        if written:
            apply_timings(self.folder, self.transcript, self.timings)
        return written

    def restamp_part(self, part_index: int, content: str) -> None:
        """Refresh digests after a part's transcript was corrected on disk."""
        part = self.transcript.part(part_index)
        if part is None:
            return
        part.sha256 = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        part.edited = True
        self.transcript.sha256 = session_digest([p.sha256 for p in self.transcript.parts])

    @property
    def media_kind(self) -> str | None:
        kinds = [p.media_kind for p in self.transcript.parts if p.media_kind]
        if not kinds:
            return None
        return "video" if "video" in kinds else "audio"

    @property
    def title(self) -> str:
        return self.folder.name or str(self.folder)

    def media_path(self, part_index: int) -> Path | None:
        if 0 <= part_index < len(self.part_files):
            return self.part_files[part_index].media_path
        return None

    def summary(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "folder": str(self.folder),
            "vtt_file": self.transcript.source_name,
            "media_file": self.transcript.parts[0].media_name if self.transcript.parts else None,
            "media_kind": self.media_kind,
            "duration": self.transcript.duration,
            "part_count": len(self.transcript.parts),
            "speakers": self.transcript.speakers,
            "quote_count": len(self.store.list()),
            # Carried so a page outside the reader can still turn a quote's
            # session time into a position in the right media file.
            "parts": [p.to_dict() for p in self.transcript.parts],
        }

    def payload(self) -> dict:
        """Everything the frontend needs to render the recording."""
        return {
            **self.summary(),
            "transcript": self.transcript.to_dict(),
            "highlights": self.store.list(),
            "highlights_stale": self.store.stale,
            "video_codes": self.video_codes.list(),
            "legacy_files": self.legacy_files,
            # How much of the session has real timings rather than interpolated
            # ones, so the reader can be honest about which it is showing.
            "timing_coverage": timing_coverage(self.transcript),
        }


def open_recording(folder: Path, books: CoderBooks | None = None) -> Recording:
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise RecordingError(f"not a directory: {folder}")

    part_files = discover_parts(folder)
    transcript, sources = build_transcript(folder, part_files)

    recording = Recording(
        id=_recording_id(folder),
        folder=folder,
        transcript=transcript,
        store=SessionQuotes({}),
        part_files=part_files,
        sources=sources,
        timings=TimingStore(folder / WORDS_FILENAME),
        video_codes=SessionVideoCodes({}),
        books=books or CoderBooks(folder),
        legacy_files=[n for n in (LEGACY_QUOTES_FILENAME, LEGACY_SPANS_FILENAME) if (folder / n).exists()],
    )
    recording.load_coders()
    return recording


def find_recordings(root: Path) -> list[Path]:
    """Recording folders at or under ``root``.

    A folder holding a transcript is itself a recording; otherwise its children
    are searched. Two levels is enough for how these arrive -- a folder of
    participant folders -- and stopping there keeps an unrelated deep tree from
    being dragged in.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return []
    if any(root.glob("*.vtt")):
        return [root]

    found: list[Path] = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        if any(child.glob("*.vtt")):
            found.append(child)
            continue
        found.extend(
            grandchild
            for grandchild in sorted(p for p in child.iterdir() if p.is_dir())
            if any(grandchild.glob("*.vtt"))
        )
    return found


class RecordingRegistry:
    """Holds open recordings, keyed by id."""

    def __init__(self, root: Path | None = None) -> None:
        self._recordings: dict[str, Recording] = {}
        self.root: Path | None = Path(root).expanduser().resolve() if root else None
        #: Folders that looked like recordings but could not be opened.
        self.failures: list[tuple[str, str]] = []
        self.coders: CoderDirectory | None = None
        self.books: CoderBooks | None = None
        if self.root is not None:
            self._open_study()

    def _open_study(self) -> None:
        """The coders, their codebooks and their themes live at the study root."""
        self.coders = CoderDirectory(self.root)
        self.books = CoderBooks(self.root)
        self._themes: dict[str, ThemeStore] = {}
        self.common_themes = CommonThemeStore(self.root)

    def theme_store(self, coder: str, readonly: bool = False) -> ThemeStore:
        """One coder's themes, from ``coders/<id>/themes.json``.

        Read-only for anyone but that coder, and then read fresh each time, since
        only their own tool writes it and it may have synced in since.
        """
        path = self.root / CODERS_DIRNAME / coder / THEMES_FILENAME
        if readonly:
            return ThemeStore(path, readonly=True)
        if coder not in self._themes:
            self._themes[coder] = ThemeStore(path)
        return self._themes[coder]

    def theme_owners(self) -> list[str]:
        """Coders who have a themes file."""
        folder = self.root / CODERS_DIRNAME
        return sorted(p.parent.name for p in folder.glob(f"*/{THEMES_FILENAME}")) if folder.is_dir() else []

    def add_folder(self, folder: Path) -> Recording:
        if self.root is None:
            self.root = Path(folder).expanduser().resolve()
            self._open_study()
        recording = open_recording(folder, self.books)
        self._recordings[recording.id] = recording
        return recording

    def refresh(self) -> None:
        """Re-read everything collaborators may have synced in: coders, codebooks, quotes, spans.

        One recording that cannot be re-read is reported, like at startup, rather
        than stopping the rest from being refreshed.
        """
        self.coders.reload()
        self.books.reload()
        self._themes.clear()
        self.common_themes.reload()
        self.failures = [f for f in self.failures if f[0] not in {r.folder.name for r in self._recordings.values()}]
        for recording in self._recordings.values():
            try:
                recording.refresh()
            except (RecordingError, VTTParseError, OSError) as exc:
                self.failures.append((recording.folder.name, str(exc)))

    def _common_sets(self):
        yield self.books.common_text
        yield self.books.common_video
        yield from self.common_themes.sets()
        for recording in self._recordings.values():
            yield from recording.common_quotes.sets()
            yield from recording.common_spans.sets()
            yield recording.returns

    def push_common(self) -> None:
        """Write the combined common records into the shared files."""
        for common in self._common_sets():
            common.push()

    def pending_common(self, coder: str) -> int:
        return sum(common.pending(coder) for common in self._common_sets())

    def common_conflicts(self) -> list[str]:
        """Sync-conflict copies next to any shared common file, as paths from the study root."""
        found = []
        for common in self._common_sets():
            for name in common.conflict_copies():
                path = common.base / name
                try:
                    found.append(str(path.relative_to(self.root)))
                except ValueError:
                    found.append(str(path))
        return sorted(set(found))

    @property
    def legacy_files(self) -> list[str]:
        """Shared files from before coders, as paths relative to the study root."""
        found = []
        for name in (LEGACY_CODEBOOK_FILENAME, LEGACY_THEMES_FILENAME):
            if self.root is not None and (self.root / name).exists():
                found.append(name)
        for recording in self.list():
            for name in recording.legacy_files:
                path = recording.folder / name
                try:
                    found.append(str(path.relative_to(self.root)))
                except ValueError:
                    found.append(str(path))
        return found

    def add_library(self, root: Path) -> list[Recording]:
        """Open every recording under a root folder.

        One unreadable folder must not take the library down with it, so
        failures are collected and reported rather than raised.
        """
        root = Path(root).expanduser().resolve()
        self.root = root
        self._open_study()
        opened: list[Recording] = []
        for folder in find_recordings(root):
            try:
                opened.append(self.add_folder(folder))
            except (RecordingError, VTTParseError, OSError) as exc:
                # A folder with an unreadable transcript is a folder to report,
                # not a reason the rest of the study cannot be opened.
                self.failures.append((folder.name, str(exc)))
        return opened

    @property
    def is_library(self) -> bool:
        return len(self._recordings) > 1 or (self.root is not None and self.root not in
                                             {r.folder for r in self._recordings.values()})

    def get(self, recording_id: str) -> Recording | None:
        return self._recordings.get(recording_id)

    def list(self) -> list[Recording]:
        return sorted(self._recordings.values(), key=lambda r: r.title.lower())

    @property
    def default(self) -> Recording | None:
        recordings = self.list()
        return recordings[0] if recordings else None
