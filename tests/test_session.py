"""Multi-part sessions: an interrupted meeting laid out on one timeline."""

import json
import struct

import pytest

from subtitle_search.mediainfo import container_duration
from subtitle_search.session import discover_parts, open_recording

from . import fixtures

PART_ONE = """WEBVTT

1
00:00:01.000 --> 00:00:05.000
Dana Whitfield: This is the first recording, before we got cut off.

2
00:00:06.000 --> 00:00:10.000
Rafael Ortiz: Agreed, and then my connection dropped.
"""

PART_TWO = """WEBVTT

1
00:00:02.000 --> 00:00:06.000
Rafael Ortiz: Okay, I'm back. Can you hear me now?

2
00:00:07.000 --> 00:00:11.000
Dana Whitfield: Loud and clear. Let's pick up where we left off.
"""

PART_THREE = """WEBVTT

1
00:00:01.500 --> 00:00:04.000
Dana Whitfield: And we got cut off again. Third time.
"""


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def write_mp4(path, seconds: float, timescale: int = 1000) -> None:
    """A container with nothing but a valid mvhd, for duration reading."""
    mvhd = b"\x00\x00\x00\x00" + struct.pack(
        ">IIII", 0, 0, timescale, int(seconds * timescale)
    ) + b"\x00" * 76
    path.write_bytes(_box(b"ftyp", b"isom" + b"\x00" * 8) + _box(b"moov", _box(b"mvhd", mvhd)))


@pytest.fixture
def interrupted(tmp_path):
    """Two recordings 10 minutes apart, Zoom cloud naming."""
    # Part one starts 14:00:00 and runs 120s; part two starts 14:12:00,
    # so the real interruption is 10 minutes.
    (tmp_path / "GMT20240301-140000_Recording.transcript.vtt").write_text(PART_ONE)
    write_mp4(tmp_path / "GMT20240301-140000_Recording_1920x1080.mp4", 120)
    (tmp_path / "GMT20240301-141200_Recording.transcript.vtt").write_text(PART_TWO)
    write_mp4(tmp_path / "GMT20240301-141200_Recording_1920x1080.mp4", 90)
    return tmp_path


def test_container_duration_is_read_without_ffprobe(tmp_path):
    path = tmp_path / "clip.mp4"
    write_mp4(path, 137.5)
    assert container_duration(path) == pytest.approx(137.5)


def test_container_duration_of_junk_is_none(tmp_path):
    path = tmp_path / "junk.mp4"
    path.write_bytes(b"not really an mp4 at all")
    assert container_duration(path) is None


def test_parts_are_ordered_and_paired_by_gmt_timestamp(interrupted):
    parts = discover_parts(interrupted)

    assert [p.vtt_path.name for p in parts] == [
        "GMT20240301-140000_Recording.transcript.vtt",
        "GMT20240301-141200_Recording.transcript.vtt",
    ]
    # Each transcript is paired with the media sharing its timestamp token.
    assert parts[0].media_path.name.startswith("GMT20240301-140000")
    assert parts[1].media_path.name.startswith("GMT20240301-141200")
    assert parts[0].started_at.hour == 14


def test_out_of_order_filenames_are_sorted_by_timestamp(tmp_path):
    """Discovery must not depend on directory listing order."""
    (tmp_path / "GMT20240301-141200_Recording.transcript.vtt").write_text(PART_TWO)
    write_mp4(tmp_path / "GMT20240301-141200_Recording.mp4", 90)
    (tmp_path / "GMT20240301-140000_Recording.transcript.vtt").write_text(PART_ONE)
    write_mp4(tmp_path / "GMT20240301-140000_Recording.mp4", 120)

    parts = discover_parts(tmp_path)
    assert parts[0].started_at < parts[1].started_at
    assert "140000" in parts[0].vtt_path.name


def test_second_part_resumes_from_first_parts_duration(interrupted):
    recording = open_recording(interrupted)
    parts = recording.transcript.parts

    assert parts[0].offset == 0
    # Part one's *recording* is 120s, so part two starts there -- not at 10s,
    # where part one's last caption ended.
    assert parts[1].offset == pytest.approx(120.0)
    assert recording.transcript.duration == pytest.approx(210.0)


def test_cue_times_are_continuous_across_parts(interrupted):
    recording = open_recording(interrupted)
    cues = recording.transcript.cues

    assert [round(c.start, 1) for c in cues] == [1.0, 6.0, 122.0, 127.0]
    # Times only ever increase; nothing resumes from zero.
    assert all(b.start >= a.start for a, b in zip(cues, cues[1:]))
    assert [c.part_index for c in cues] == [0, 0, 1, 1]


def test_real_interruption_is_measured_from_wall_clock(interrupted):
    recording = open_recording(interrupted)
    parts = recording.transcript.parts

    assert parts[0].gap_before is None
    # 14:12:00 minus (14:00:00 + 120s) = 10 minutes of real downtime, even though
    # the timeline itself stays continuous.
    assert parts[1].gap_before == pytest.approx(600.0)


def test_chunks_never_merge_across_a_part_boundary(tmp_path):
    """Part two opens with the same speaker part one closed with."""
    (tmp_path / "GMT20240301-140000_Recording.vtt").write_text(
        "WEBVTT\n\n1\n00:00:01.000 --> 00:00:05.000\nDana Whitfield: Before the drop.\n"
    )
    write_mp4(tmp_path / "GMT20240301-140000_Recording.mp4", 30)
    (tmp_path / "GMT20240301-140100_Recording.vtt").write_text(
        "WEBVTT\n\n1\n00:00:01.000 --> 00:00:05.000\nDana Whitfield: After the drop.\n"
    )
    write_mp4(tmp_path / "GMT20240301-140100_Recording.mp4", 30)

    transcript = open_recording(tmp_path).transcript
    assert len(transcript.chunks) == 2
    assert [c.part_index for c in transcript.chunks] == [0, 1]
    assert [c.starts_part for c in transcript.chunks] == [True, True]


def test_three_parts_accumulate(tmp_path):
    for stamp, content, seconds in (
        ("140000", PART_ONE, 60),
        ("140500", PART_TWO, 30),
        ("141000", PART_THREE, 20),
    ):
        (tmp_path / f"GMT20240301-{stamp}_Recording.vtt").write_text(content)
        write_mp4(tmp_path / f"GMT20240301-{stamp}_Recording.mp4", seconds)

    parts = open_recording(tmp_path).transcript.parts
    assert [p.offset for p in parts] == [0, 60, 90]
    assert [round(p.duration) for p in parts] == [60, 30, 20]


def test_missing_media_falls_back_to_caption_end(tmp_path):
    (tmp_path / "GMT20240301-140000_Recording.vtt").write_text(PART_ONE)
    (tmp_path / "GMT20240301-140500_Recording.vtt").write_text(PART_TWO)

    parts = open_recording(tmp_path).transcript.parts
    assert parts[0].media_name is None
    # Part one's last caption ends at 10s, plus a small tail so part two does not
    # start on top of its final word.
    assert parts[1].offset == pytest.approx(11.0)


def test_single_transcript_folder_still_works(tmp_path):
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    write_mp4(tmp_path / "meeting.mp4", 60)

    recording = open_recording(tmp_path)
    assert len(recording.transcript.parts) == 1
    assert recording.transcript.parts[0].offset == 0
    assert recording.transcript.cues[0].start == pytest.approx(2.18)
    assert recording.store.list() == []


def test_generic_filenames_pair_by_stem(tmp_path):
    (tmp_path / "zoom_0.vtt").write_text(PART_ONE)
    write_mp4(tmp_path / "zoom_0.mp4", 60)
    (tmp_path / "zoom_1.vtt").write_text(PART_TWO)
    write_mp4(tmp_path / "zoom_1.mp4", 30)

    parts = discover_parts(tmp_path)
    assert [p.vtt_path.name for p in parts] == ["zoom_0.vtt", "zoom_1.vtt"]
    assert parts[0].media_path.name == "zoom_0.mp4"
    assert parts[1].media_path.name == "zoom_1.mp4"


def test_old_shared_files_are_reported_and_left_alone(tmp_path):
    """Files from before coders are neither loaded nor touched, only reported."""
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    old = tmp_path / "session.highlights.json"
    old.write_text(json.dumps({"version": 1, "highlights": [{"id": "old123", "text": "x"}]}))
    (tmp_path / "session.video_codes.json").write_text("{}")

    recording = open_recording(tmp_path)

    assert recording.store.list() == []
    assert sorted(recording.legacy_files) == ["session.highlights.json", "session.video_codes.json"]
    assert json.loads(old.read_text())["highlights"][0]["id"] == "old123"


def _quote():
    return {"text": "share my screen", "start_cue_id": "c0", "start_char_offset": 27,
            "end_cue_id": "c0", "end_char_offset": 42}


def test_a_coders_quotes_live_in_their_folder_inside_the_recording(tmp_path):
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    recording = open_recording(tmp_path)

    recording.store.create("ann", _quote())

    assert (tmp_path / "coders" / "ann" / "quotes.json").is_file()
    assert [q["coder"] for q in open_recording(tmp_path).store.list()] == ["ann"]


def test_a_quote_may_only_carry_its_coders_own_text_codes(tmp_path):
    from subtitle_search.highlights import HighlightError
    from subtitle_search.session import CoderBooks

    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    books = CoderBooks(tmp_path)
    mine = books.text("ann").add({"name": "trust"})
    theirs = books.text("ben").add({"name": "trust"})
    recording = open_recording(tmp_path, books)

    assert recording.store.create("ann", {**_quote(), "codes": [mine["id"]]})["codes"] == [mine["id"]]
    with pytest.raises(HighlightError):
        recording.store.create("ann", {**_quote(), "codes": [theirs["id"]]})


def test_refresh_picks_up_a_coder_who_synced_in_since_opening(tmp_path):
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    recording = open_recording(tmp_path)
    other = open_recording(tmp_path)  # a collaborator's copy, writing the same folder
    other.store.create("ben", _quote())

    assert recording.store.list() == []
    recording.refresh()
    assert [q["coder"] for q in recording.store.list()] == ["ben"]


def test_single_part_digest_matches_plain_content(tmp_path):
    """A folder that predates parts must not read as 'the transcript changed'."""
    from subtitle_search.vtt import parse_vtt

    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    recording = open_recording(tmp_path)

    assert recording.transcript.sha256 == parse_vtt(fixtures.COLON_PREFIX).sha256
    assert recording.store.stale is False


def test_a_correction_never_loses_quotes_a_collaborator_synced_in(tmp_path):
    """Ben's tool loaded Ann's quotes earlier; Ann has saved more since. Ben correcting
    a caption must not write his stale copy of Ann's file back over hers."""
    from subtitle_search.editing import apply_cue_edit

    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    ann = open_recording(tmp_path)
    ann.store.create("ann", _quote())
    ben = open_recording(tmp_path)  # sees Ann's one quote
    ann.store.create("ann", {**_quote(), "text": "second"})  # synced in after Ben loaded

    cue = ben.transcript.cue("c2")
    apply_cue_edit(ben, "c2", cue.text + " Indeed.")

    saved = json.loads((tmp_path / "coders" / "ann" / "quotes.json").read_text())["highlights"]
    assert len(saved) == 2



def test_reading_an_unreadable_coder_file_leaves_it_where_it_is(tmp_path):
    """A half-synced file in someone's folder is theirs; opening the study must not
    rename it away, or the sync client deletes it from their folder."""
    from subtitle_search.coders import CoderDirectory
    from subtitle_search.session import CoderBooks

    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    folder = tmp_path / "coders" / "cat"
    folder.mkdir(parents=True)
    for name in ("quotes.json", "video_codes.json", "coder.json", "text_codebook.json"):
        (folder / name).write_text("{half synced")

    recording = open_recording(tmp_path)
    CoderDirectory(tmp_path).list()
    CoderBooks(tmp_path).all_text()
    recording.store.restamp()

    assert sorted(p.name for p in folder.iterdir()) == ["coder.json", "quotes.json", "text_codebook.json", "video_codes.json"]
    assert (folder / "quotes.json").read_text() == "{half synced"


def test_your_own_unreadable_file_is_still_kept_aside_before_you_write(tmp_path):
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    mine = tmp_path / "coders" / "ann" / "quotes.json"
    mine.parent.mkdir(parents=True)
    mine.write_text("{broken")

    recording = open_recording(tmp_path)
    recording.store.create("ann", _quote())

    assert (mine.parent / "quotes.json.corrupt").read_text() == "{broken"
    assert len(json.loads(mine.read_text())["highlights"]) == 1
