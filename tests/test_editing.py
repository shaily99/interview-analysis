"""Correcting the transcript, and keeping quotes anchored while it happens."""

import pytest

from subtitle_search.editing import (
    EditError,
    apply_cue_edit,
    apply_cue_merge,
    apply_cue_split,
    apply_selection_speaker,
    apply_speaker_edit,
    backup_path,
    remap_offset,
)
from subtitle_search.session import open_recording

from .test_session import write_mp4

EDITABLE = """WEBVTT

1
00:00:01.000 --> 00:00:08.000
Dana Whitfield: We should reconvene about the kubernetes migration next week.

2
00:00:09.000 --> 00:00:15.000
Rafael Ortiz: Agreed. I'll put something on the calendar for Tuesday.

3
00:00:16.000 --> 00:00:21.000
Dana Whitfield: Perfect, thank you.
"""

# A block the parser cannot read, sitting between two it can. Regenerating the
# file from parsed cues would silently drop it; splicing must not.
WITH_UNPARSEABLE = """WEBVTT

1
00:00:01.000 --> 00:00:08.000
Dana Whitfield: The first line, which parses fine.

2
00:00:09:000 --> 00:00:15.000
Dana Whitfield: This block has a malformed timestamp and gets skipped.

3
00:00:16.000 --> 00:00:21.000
Rafael Ortiz: The third line, which also parses fine.
"""


@pytest.fixture
def recording(tmp_path):
    (tmp_path / "meeting.vtt").write_text(EDITABLE, encoding="utf-8")
    write_mp4(tmp_path / "meeting.mp4", 30)
    return open_recording(tmp_path)


# -- offset remapping ---------------------------------------------------


def test_remap_offset_through_an_insertion():
    old, new = "hello world", "hello brave world"
    # "world" starts at 6 in the old text and 12 in the new one.
    assert old[6:] == "world"
    assert remap_offset(old, new, 6) == 12
    assert remap_offset(old, new, 0) == 0
    assert remap_offset(old, new, len(old)) == len(new)


def test_remap_offset_through_a_deletion():
    old, new = "the very big dog", "the big dog"
    assert remap_offset(old, new, old.index("big")) == new.index("big")


def test_remap_offset_inside_a_rewritten_span_collapses_to_an_edge():
    old, new = "kubernetes", "Kubernetes"
    mapped = remap_offset(old, new, 5)
    assert 0 <= mapped <= len(new)


def test_remap_offset_clamps_out_of_range():
    assert remap_offset("abc", "abcdef", 99) == 6
    assert remap_offset("abc", "ab", -5) == 0


# -- writing back -------------------------------------------------------


def test_edit_writes_to_the_original_file(recording):
    apply_cue_edit(recording, "c0", "We should reconvene about the Kubernetes migration next week.")

    saved = (recording.folder / "meeting.vtt").read_text()
    assert "Kubernetes migration" in saved
    # The speaker prefix is put back exactly as it was.
    assert "Dana Whitfield: We should reconvene about the Kubernetes" in saved
    assert recording.transcript.cue("c0").text.startswith("We should reconvene about the Kubernetes")


def test_first_edit_backs_up_the_original(recording):
    backup = backup_path(recording.folder / "meeting.vtt")
    assert not backup.exists()

    result = apply_cue_edit(recording, "c0", "First correction.")
    assert result["backup_created"] == "meeting_original.vtt"
    assert backup.read_text() == EDITABLE


def test_backup_is_never_overwritten_by_later_edits(recording):
    """The backup holds the file as it arrived, not the last state before an edit."""
    apply_cue_edit(recording, "c0", "First correction.")
    second = apply_cue_edit(recording, "c1", "Second correction.")

    assert second["backup_created"] is None
    assert backup_path(recording.folder / "meeting.vtt").read_text() == EDITABLE


def test_backup_is_not_read_as_another_recording(recording):
    """The backup shares the folder and the .vtt extension as the transcript."""
    before = len(recording.transcript.cues)
    apply_cue_edit(recording, "c0", "An edit, which creates the backup.")

    reopened = open_recording(recording.folder)
    assert backup_path(recording.folder / "meeting.vtt").exists()
    # Without excluding it, discovery would treat the backup as a second part
    # and double every count.
    assert len(reopened.transcript.parts) == 1
    assert len(reopened.transcript.cues) == before
    assert reopened.transcript.duration == recording.transcript.duration


def test_untouched_lines_are_left_byte_identical(recording):
    original = (recording.folder / "meeting.vtt").read_text()
    apply_cue_edit(recording, "c1", "Agreed. Calendar invite going out now.")
    saved = (recording.folder / "meeting.vtt").read_text()

    for line in ("WEBVTT", "00:00:01.000 --> 00:00:08.000", "Dana Whitfield: Perfect, thank you."):
        assert line in saved
    # Only the edited payload differs.
    assert original.count("\n") == saved.count("\n")


def test_a_block_the_parser_skipped_survives_an_edit(tmp_path):
    (tmp_path / "meeting.vtt").write_text(WITH_UNPARSEABLE, encoding="utf-8")
    recording = open_recording(tmp_path)

    # Only two cues parsed; the malformed block was skipped.
    assert len(recording.transcript.cues) == 2
    apply_cue_edit(recording, "c0", "The first line, now corrected.")

    saved = (tmp_path / "meeting.vtt").read_text()
    assert "This block has a malformed timestamp and gets skipped." in saved
    assert "00:00:09:000 --> 00:00:15.000" in saved


def test_consecutive_edits_shift_later_spans_correctly(recording):
    """The second edit must land correctly after the first changed the length."""
    apply_cue_edit(recording, "c0", "Short.")
    apply_cue_edit(recording, "c1", "Also short.")
    apply_cue_edit(recording, "c2", "A considerably longer replacement line than before.")

    saved = (recording.folder / "meeting.vtt").read_text()
    assert "Dana Whitfield: Short." in saved
    assert "Rafael Ortiz: Also short." in saved
    assert "Dana Whitfield: A considerably longer replacement line than before." in saved
    assert saved.count("-->") == 3


def test_crlf_line_endings_are_preserved(tmp_path):
    (tmp_path / "meeting.vtt").write_bytes(EDITABLE.replace("\n", "\r\n").encode("utf-8"))
    recording = open_recording(tmp_path)

    apply_cue_edit(recording, "c1", "Corrected on a CRLF file.")
    saved = (tmp_path / "meeting.vtt").read_bytes()

    assert b"Corrected on a CRLF file." in saved
    assert saved.count(b"\r\n") >= 8  # the rest of the file kept its endings


def test_empty_edit_is_rejected(recording):
    with pytest.raises(EditError):
        apply_cue_edit(recording, "c0", "   ")


def test_unknown_cue_is_rejected(recording):
    with pytest.raises(EditError):
        apply_cue_edit(recording, "c999", "text")


def test_unchanged_text_is_a_no_op(recording):
    text = recording.transcript.cue("c0").text
    result = apply_cue_edit(recording, "c0", text)

    assert result["changed"] is False
    assert not backup_path(recording.folder / "meeting.vtt").exists()


# -- quotes surviving edits ---------------------------------------------


def _quote(recording, cue_id, start, end):
    cue = recording.transcript.cue(cue_id)
    return recording.store.create(
        "tester",
        {
            "text": cue.text[start:end],
            "start_cue_id": cue_id,
            "start_char_offset": start,
            "end_cue_id": cue_id,
            "end_char_offset": end,
        }
    )


def test_quote_after_an_edit_still_covers_the_same_words(recording):
    cue = recording.transcript.cue("c0")
    start = cue.text.index("kubernetes")
    quote = _quote(recording, "c0", start, start + len("kubernetes migration"))
    assert quote["text"] == "kubernetes migration"

    # Insert words *before* the quote; its offsets must move with it.
    apply_cue_edit(
        recording, "c0", "So, we should probably reconvene about the kubernetes migration next week."
    )

    updated = recording.store.list()[0]
    covered = recording.transcript.text_between(
        updated["start_cue_id"],
        updated["start_char_offset"],
        updated["end_cue_id"],
        updated["end_char_offset"],
    )
    assert covered == "kubernetes migration"
    assert updated["text"] == "kubernetes migration"


def test_correcting_a_word_inside_a_quote_updates_the_saved_quote(recording):
    cue = recording.transcript.cue("c0")
    start = cue.text.index("kubernetes")
    _quote(recording, "c0", start, start + len("kubernetes migration"))

    apply_cue_edit(
        recording, "c0", "We should reconvene about the Kubernetes migration next week."
    )

    updated = recording.store.list()[0]
    # The saved quote reflects the correction rather than preserving the error.
    assert "Kubernetes" in updated["text"]
    assert "kubernetes" not in updated["text"]


def test_quote_times_are_re_resolved_after_an_edit(recording):
    quote = _quote(recording, "c0", 0, 10)
    before = quote["start_time"]

    apply_cue_edit(recording, "c0", "Padding words at the front. " + recording.transcript.cue("c0").text)

    updated = recording.store.list()[0]
    assert updated["start_time"] >= before
    cue = recording.transcript.cue("c0")
    assert cue.start <= updated["start_time"] <= cue.end


def test_quotes_in_other_cues_are_untouched(recording):
    other = _quote(recording, "c2", 0, 7)
    snapshot = dict(other)

    apply_cue_edit(recording, "c0", "A totally different first line entirely.")

    unchanged = next(h for h in recording.store.list() if h["id"] == snapshot["id"])
    assert unchanged["start_char_offset"] == snapshot["start_char_offset"]
    assert unchanged["text"] == snapshot["text"]


def test_transcript_does_not_read_as_stale_after_its_own_edit(recording):
    _quote(recording, "c0", 0, 10)
    apply_cue_edit(recording, "c0", "An edited first line.")

    assert recording.store.stale is False


# -- multi-part ---------------------------------------------------------


def test_edit_lands_in_the_right_part(tmp_path):
    (tmp_path / "GMT20240301-140000_Recording.vtt").write_text(EDITABLE)
    write_mp4(tmp_path / "GMT20240301-140000_Recording.mp4", 60)
    (tmp_path / "GMT20240301-141200_Recording.vtt").write_text(
        "WEBVTT\n\n1\n00:00:01.000 --> 00:00:05.000\nRafael Ortiz: Second recording line.\n"
    )
    write_mp4(tmp_path / "GMT20240301-141200_Recording.mp4", 30)

    recording = open_recording(tmp_path)
    second_part_cue = next(c for c in recording.transcript.cues if c.part_index == 1)
    apply_cue_edit(recording, second_part_cue.id, "Second recording line, corrected.")

    assert "corrected" in (tmp_path / "GMT20240301-141200_Recording.vtt").read_text()
    assert "corrected" not in (tmp_path / "GMT20240301-140000_Recording.vtt").read_text()
    # Only the edited part gets a backup.
    assert (tmp_path / "GMT20240301-141200_Recording_original.vtt").exists()
    assert not (tmp_path / "GMT20240301-140000_Recording_original.vtt").exists()


def test_voice_tag_files_keep_their_tags(tmp_path):
    (tmp_path / "meeting.vtt").write_text(
        "WEBVTT\n\n1\n00:00:01.000 --> 00:00:05.000\n<v Dana Whitfield>Original wording here.\n"
        "\n2\n00:00:06.000 --> 00:00:09.000\n<v Rafael Ortiz>Another line.\n"
    )
    recording = open_recording(tmp_path)
    apply_cue_edit(recording, "c0", "Corrected wording here.")

    saved = (tmp_path / "meeting.vtt").read_text()
    assert "<v Dana Whitfield>Corrected wording here." in saved


# -- saying who actually said it ---------------------------------------

ONE_VOICE = """WEBVTT

1
00:00:01.000 --> 00:00:06.000
Dana Whitfield: So tell me how you approach it.

2
00:00:07.000 --> 00:00:13.000
Dana Whitfield: Honestly I read the whole thing first.

3
00:00:14.000 --> 00:00:20.000
Dana Whitfield: And what breaks down in that?

4
00:00:21.000 --> 00:00:27.000
Dana Whitfield: Finding where a quote actually is.
"""


@pytest.fixture
def one_voice(tmp_path):
    (tmp_path / "meeting.vtt").write_text(ONE_VOICE, encoding="utf-8")
    write_mp4(tmp_path / "meeting.mp4", 60)
    return open_recording(tmp_path)


def test_a_single_speaker_transcript_is_not_joined(one_voice):
    """One label on an in-person recording covers a whole room, not one person."""
    assert one_voice.transcript.speakers == ["Dana Whitfield"]
    assert len(one_voice.transcript.chunks) == 4


def test_reassigning_a_line_writes_it_to_the_transcript(one_voice):
    apply_speaker_edit(one_voice, "c1", "Rafael Ortiz")

    saved = (one_voice.folder / "meeting.vtt").read_text()
    assert "Rafael Ortiz: Honestly I read the whole thing first." in saved
    assert one_voice.transcript.cue("c1").speaker == "Rafael Ortiz"


def test_blocks_reform_once_a_second_speaker_exists(one_voice):
    """Assigning speakers is what makes joining meaningful again."""
    apply_speaker_edit(one_voice, "c1", "Rafael Ortiz")
    apply_speaker_edit(one_voice, "c3", "Rafael Ortiz")

    transcript = one_voice.transcript
    assert transcript.speakers == ["Dana Whitfield", "Rafael Ortiz"]
    assert [c.cue_ids for c in transcript.chunks] == [["c0"], ["c1"], ["c2"], ["c3"]]


def test_a_run_of_lines_can_be_reassigned_at_once(one_voice):
    """A mis-segmented answer is usually several captions long."""
    apply_speaker_edit(one_voice, "c1", "Rafael Ortiz", through_cue_id="c3")

    speakers = [one_voice.transcript.cue(f"c{i}").speaker for i in range(4)]
    assert speakers == ["Dana Whitfield", "Rafael Ortiz", "Rafael Ortiz", "Rafael Ortiz"]
    # And those three now read as one turn.
    assert [c.cue_ids for c in one_voice.transcript.chunks] == [["c0"], ["c1", "c2", "c3"]]


def test_a_reversed_range_still_works(one_voice):
    apply_speaker_edit(one_voice, "c3", "Rafael Ortiz", through_cue_id="c1")
    assert one_voice.transcript.cue("c2").speaker == "Rafael Ortiz"


def test_a_one_word_name_survives_reparsing(one_voice):
    """Detection would never accept it; the roster makes it a decision, not a guess."""
    apply_speaker_edit(one_voice, "c1", "Interviewer")

    assert "NOTE speakers:" in (one_voice.folder / "meeting.vtt").read_text()
    assert one_voice.transcript.cue("c1").speaker == "Interviewer"

    # And it holds when the folder is opened fresh.
    assert open_recording(one_voice.folder).transcript.cue("c1").speaker == "Interviewer"


def test_reassigning_backs_up_the_original_first(one_voice):
    result = apply_speaker_edit(one_voice, "c1", "Rafael Ortiz")
    assert result["backup_created"] == "meeting_original.vtt"
    assert backup_path(one_voice.folder / "meeting.vtt").read_text() == ONE_VOICE


def test_reassigning_leaves_the_words_alone(one_voice):
    apply_speaker_edit(one_voice, "c2", "Rafael Ortiz")
    assert one_voice.transcript.cue("c2").text == "And what breaks down in that?"


def test_quotes_stay_anchored_across_a_reassignment(one_voice):
    quote = one_voice.store.create(
        "tester",
        {
            "text": "read the whole thing",
            "start_cue_id": "c1",
            "start_char_offset": 11,
            "end_cue_id": "c1",
            "end_char_offset": 31,
        }
    )
    apply_speaker_edit(one_voice, "c1", "Rafael Ortiz")

    kept = one_voice.store.list()[0]
    assert kept["id"] == quote["id"]
    assert one_voice.transcript.text_between(
        kept["start_cue_id"], kept["start_char_offset"],
        kept["end_cue_id"], kept["end_char_offset"],
    ) == "read the whole thing"
    assert one_voice.store.stale is False


def test_an_empty_speaker_is_rejected(one_voice):
    with pytest.raises(EditError):
        apply_speaker_edit(one_voice, "c1", "   ")


def test_a_name_with_a_colon_is_rejected(one_voice):
    """It would be re-read as a label plus text on the next parse."""
    with pytest.raises(EditError):
        apply_speaker_edit(one_voice, "c1", "Rafael: Ortiz")


def test_an_unknown_line_is_rejected(one_voice):
    with pytest.raises(EditError):
        apply_speaker_edit(one_voice, "c99", "Rafael Ortiz")


def test_a_line_with_no_label_can_be_given_one(tmp_path):
    """Zoom leaves trailing captions unattributed; they inherit the wrong person."""
    (tmp_path / "meeting.vtt").write_text(
        "WEBVTT\n\n1\n00:00:01.000 --> 00:00:05.000\nDana Whitfield: A labelled line.\n\n"
        "2\n00:00:06.000 --> 00:00:10.000\nA trailing clause with no label at all.\n",
        encoding="utf-8",
    )
    recording = open_recording(tmp_path)
    assert recording.transcript.cue("c1").speaker == "Dana Whitfield"  # inherited

    apply_speaker_edit(recording, "c1", "Rafael Ortiz")
    assert recording.transcript.cue("c1").speaker == "Rafael Ortiz"
    assert "Rafael Ortiz: A trailing clause" in (tmp_path / "meeting.vtt").read_text()


# -- splitting a caption two people share ------------------------------

# The failure this exists for: Zoom filed the end of one turn and the start of
# the next under one label, so no reattribution can separate them.
INTERMINGLED = """WEBVTT

1
00:00:00.000 --> 00:00:04.000
Dana Whitfield: Thanks for making the time.

2
00:00:04.000 --> 00:00:14.000
Dana Whitfield: So walk me through it. Sure, I read the whole thing first.

3
00:00:14.000 --> 00:00:20.000
Dana Whitfield: And what breaks down there?
"""


@pytest.fixture
def intermingled(tmp_path):
    (tmp_path / "meeting.vtt").write_text(INTERMINGLED, encoding="utf-8")
    write_mp4(tmp_path / "meeting.mp4", 30)
    return open_recording(tmp_path)


def _cut_before(recording, cue_id, word, **kwargs):
    cue = recording.transcript.cue(cue_id)
    return apply_cue_split(recording, cue_id, cue.text.index(word), **kwargs)


def test_a_split_turns_one_caption_into_two(intermingled):
    _cut_before(intermingled, "c1", "Sure")
    texts = [cue.text for cue in intermingled.transcript.cues]
    assert texts == [
        "Thanks for making the time.",
        "So walk me through it.",
        "Sure, I read the whole thing first.",
        "And what breaks down there?",
    ]


def test_both_halves_keep_the_label_so_the_file_re_parses(intermingled):
    _cut_before(intermingled, "c1", "Sure")
    reread = open_recording(intermingled.folder)
    assert [cue.speaker for cue in reread.transcript.cues] == ["Dana Whitfield"] * 4


def test_the_halves_meet_at_an_interpolated_boundary(intermingled):
    result = _cut_before(intermingled, "c1", "Sure")
    head, tail = intermingled.transcript.cue("c1"), intermingled.transcript.cue("c2")
    assert head.start == 4.0
    assert tail.end == 14.0
    assert head.end == tail.start == result["at"]
    # Two thirds of the words are in the tail, so the cut lands early in the cue.
    assert 4.0 < result["at"] < 9.0


def test_neither_half_can_be_empty(intermingled):
    with pytest.raises(EditError):
        apply_cue_split(intermingled, "c1", 0)
    with pytest.raises(EditError):
        apply_cue_split(intermingled, "c1", len(intermingled.transcript.cue("c1").text))


def test_splitting_an_unknown_line_is_rejected(intermingled):
    with pytest.raises(EditError):
        apply_cue_split(intermingled, "c99", 5)


def test_splitting_backs_up_the_original_first(intermingled):
    result = _cut_before(intermingled, "c1", "Sure")
    assert result["backup_created"] == "meeting_original.vtt"
    assert backup_path(intermingled.folder / "meeting.vtt").read_text() == INTERMINGLED


def test_untouched_captions_are_left_byte_identical(intermingled):
    _cut_before(intermingled, "c1", "Sure")
    written = (intermingled.folder / "meeting.vtt").read_text()
    assert written.startswith("WEBVTT\n\n1\n00:00:00.000 --> 00:00:04.000\n")
    assert "00:00:14.000 --> 00:00:20.000\nDana Whitfield: And what breaks down there?" in written


def test_the_halves_can_then_be_given_different_speakers(intermingled):
    """The point of the whole exercise."""
    _cut_before(intermingled, "c1", "Sure")
    apply_speaker_edit(intermingled, "c2", "Rafael Ortiz")
    assert intermingled.transcript.cue("c1").speaker == "Dana Whitfield"
    assert intermingled.transcript.cue("c2").speaker == "Rafael Ortiz"


def test_the_offset_is_carried_across_unsaved_typing(intermingled):
    """The caret sits in the editor's text, which may not match the saved line."""
    edited = "So walk me through it, then. Sure, I read the whole thing first."
    # 'Sure' is seven characters further along in the editor than in the file, so
    # cutting at the raw offset would strand the end of the question in the tail.
    apply_cue_split(intermingled, "c1", edited.index("Sure"), against=edited)
    assert intermingled.transcript.cue("c1").text == "So walk me through it."
    assert intermingled.transcript.cue("c2").text.startswith("Sure,")


# -- quotes surviving a split ------------------------------------------


def test_a_quote_after_the_split_still_covers_the_same_words(intermingled):
    quote = _quote(intermingled, "c2", 4, 9)
    assert quote["text"] == "what"

    _cut_before(intermingled, "c1", "Sure")
    assert quote["start_cue_id"] == "c3"
    assert intermingled.transcript.cue("c3").text[4:9] == "what "
    assert quote["text"].strip() == "what"


def test_a_quote_before_the_split_is_untouched(intermingled):
    quote = _quote(intermingled, "c0", 0, 6)
    before = dict(quote)
    _cut_before(intermingled, "c1", "Sure")
    assert quote == before


def test_a_quote_in_the_first_half_keeps_its_words(intermingled):
    cue = intermingled.transcript.cue("c1")
    start = cue.text.index("walk")
    quote = _quote(intermingled, "c1", start, start + len("walk me through"))

    _cut_before(intermingled, "c1", "Sure")
    assert quote["start_cue_id"] == "c1"
    assert quote["text"] == "walk me through"


def test_a_quote_in_the_second_half_moves_to_the_new_caption(intermingled):
    cue = intermingled.transcript.cue("c1")
    start = cue.text.index("read the whole thing")
    quote = _quote(intermingled, "c1", start, start + len("read the whole thing"))

    _cut_before(intermingled, "c1", "Sure")
    assert quote["start_cue_id"] == "c2"
    assert quote["text"] == "read the whole thing"


def test_a_quote_spanning_the_cut_still_covers_the_same_words(intermingled):
    cue = intermingled.transcript.cue("c1")
    start = cue.text.index("through it")
    quote = _quote(intermingled, "c1", start, cue.text.index("read"))
    assert quote["text"] == "through it. Sure, I"

    _cut_before(intermingled, "c1", "Sure")
    assert (quote["start_cue_id"], quote["end_cue_id"]) == ("c1", "c2")
    assert quote["text"] == "through it. Sure, I"


def test_a_split_moves_the_quote_times_with_the_captions(intermingled):
    quote = _quote(intermingled, "c2", 0, 10)
    was = quote["start_time"]
    _cut_before(intermingled, "c1", "Sure")
    assert quote["start_cue_id"] == "c3"
    assert quote["start_time"] == was  # same caption, same place in the recording


def test_the_transcript_does_not_read_as_stale_after_a_split(intermingled):
    _quote(intermingled, "c0", 0, 6)
    _cut_before(intermingled, "c1", "Sure")
    assert not intermingled.store.stale


def test_a_split_in_a_later_part_writes_that_file_own_timeline(tmp_path):
    """Cue times are session times; a file's timestamps start from zero."""
    (tmp_path / "GMT20240301-140000_Recording.vtt").write_text(EDITABLE)
    write_mp4(tmp_path / "GMT20240301-140000_Recording.mp4", 60)
    (tmp_path / "GMT20240301-141200_Recording.vtt").write_text(
        "WEBVTT\n\n1\n00:00:02.000 --> 00:00:12.000\n"
        "Rafael Ortiz: That is where we left it. Right, let us pick it up.\n"
    )
    write_mp4(tmp_path / "GMT20240301-141200_Recording.mp4", 30)

    recording = open_recording(tmp_path)
    cue = next(c for c in recording.transcript.cues if c.part_index == 1)
    assert cue.start > 60  # sits well down the session timeline

    apply_cue_split(recording, cue.id, cue.text.index("Right"))
    written = (tmp_path / "GMT20240301-141200_Recording.vtt").read_text()
    assert "00:00:02.000 --> 00:00:07.098" in written
    assert "00:00:07.098 --> 00:00:12.000" in written

    # And the session times still land where they did, one part along.
    reread = open_recording(tmp_path)
    halves = [c for c in reread.transcript.cues if c.part_index == 1]
    assert [round(c.start, 3) for c in halves] == [cue.start, round(cue.start + 5.098, 3)]


# -- putting captions back together -------------------------------------

# Zoom's other failure: one sentence across three captions, so a quote that
# reads as a single thought is three anchors underneath.
CHOPPED = """WEBVTT

1
00:00:00.000 --> 00:00:02.000
Dana Whitfield: One sentence

2
00:00:02.000 --> 00:00:04.000
Dana Whitfield: chopped across

3
00:00:04.000 --> 00:00:06.500
Dana Whitfield: three captions.

4
00:00:07.000 --> 00:00:09.000
Rafael Ortiz: And then a reply.
"""


@pytest.fixture
def chopped(tmp_path):
    (tmp_path / "meeting.vtt").write_text(CHOPPED, encoding="utf-8")
    write_mp4(tmp_path / "meeting.mp4", 30)
    return open_recording(tmp_path)


def test_a_run_of_captions_becomes_one(chopped):
    result = apply_cue_merge(chopped, "c0", "c2")
    assert result["joined"] == 3
    assert result["cue_id"] == "c0"
    assert [cue.text for cue in chopped.transcript.cues] == [
        "One sentence chopped across three captions.",
        "And then a reply.",
    ]


def test_the_joined_caption_spans_the_whole_run(chopped):
    apply_cue_merge(chopped, "c0", "c2")
    joined = chopped.transcript.cue("c0")
    assert (joined.start, joined.end) == (0.0, 6.5)


def test_joining_leaves_the_other_captions_alone(chopped):
    apply_cue_merge(chopped, "c0", "c2")
    written = (chopped.folder / "meeting.vtt").read_text()
    # The surviving block keeps its identifier; the absorbed ones lose theirs.
    assert "1\n00:00:00.000 --> 00:00:06.500" in written
    assert "4\n00:00:07.000 --> 00:00:09.000\nRafael Ortiz: And then a reply." in written
    assert "\n2\n" not in written and "\n3\n" not in written


def test_a_reversed_range_joins_the_same_way(chopped):
    assert apply_cue_merge(chopped, "c2", "c0")["joined"] == 3


def test_joining_one_caption_to_itself_is_rejected(chopped):
    with pytest.raises(EditError):
        apply_cue_merge(chopped, "c1", "c1")


def test_joining_an_unknown_caption_is_rejected(chopped):
    with pytest.raises(EditError):
        apply_cue_merge(chopped, "c0", "c99")


def test_joining_backs_up_the_original_first(chopped):
    assert apply_cue_merge(chopped, "c0", "c1")["backup_created"] == "meeting_original.vtt"
    assert backup_path(chopped.folder / "meeting.vtt").read_text() == CHOPPED


def test_joining_across_recordings_is_rejected(tmp_path):
    (tmp_path / "GMT20240301-140000_Recording.vtt").write_text(EDITABLE)
    write_mp4(tmp_path / "GMT20240301-140000_Recording.mp4", 60)
    (tmp_path / "GMT20240301-141200_Recording.vtt").write_text(
        "WEBVTT\n\n1\n00:00:01.000 --> 00:00:05.000\nRafael Ortiz: Second recording.\n"
    )
    write_mp4(tmp_path / "GMT20240301-141200_Recording.mp4", 30)
    recording = open_recording(tmp_path)
    last_of_first = [c for c in recording.transcript.cues if c.part_index == 0][-1]
    first_of_second = [c for c in recording.transcript.cues if c.part_index == 1][0]

    with pytest.raises(EditError, match="two different recordings"):
        apply_cue_merge(recording, last_of_first.id, first_of_second.id)


def test_the_first_speaker_survives_and_the_rest_are_reported(chopped):
    """One caption carries one label, so a join across two of them drops a name."""
    apply_speaker_edit(chopped, "c1", "Rafael Ortiz")
    result = apply_cue_merge(chopped, "c0", "c1")

    assert result["speaker"] == "Dana Whitfield"
    assert result["absorbed_speakers"] == ["Rafael Ortiz"]
    assert chopped.transcript.cue("c0").speaker == "Dana Whitfield"


# -- undo, and the guard that makes it safe -----------------------------


def test_a_split_can_be_undone(intermingled):
    before = [cue.text for cue in intermingled.transcript.cues]
    cue = intermingled.transcript.cue("c1")
    split = apply_cue_split(intermingled, "c1", cue.text.index("Sure"), align=False)

    apply_cue_merge(intermingled, *split["cue_ids"], expect=split["halves"])
    assert [cue.text for cue in intermingled.transcript.cues] == before


def test_undo_refuses_once_the_transcript_has_moved(intermingled):
    """Cue ids are positional, so an undo pressed late must not join the wrong two."""
    cue = intermingled.transcript.cue("c1")
    split = apply_cue_split(intermingled, "c1", cue.text.index("Sure"), align=False)
    # Something else happened in between.
    apply_cue_edit(intermingled, "c1", "So walk me through it, would you.")

    with pytest.raises(EditError, match="has changed since then"):
        apply_cue_merge(intermingled, *split["cue_ids"], expect=split["halves"])
    # And nothing was joined.
    assert len(intermingled.transcript.cues) == 4


def test_an_expectation_that_matches_is_honoured(chopped):
    apply_cue_merge(chopped, "c0", "c1", expect=["One sentence", "chopped across"])
    assert chopped.transcript.cue("c0").text == "One sentence chopped across"


# -- quotes surviving a join --------------------------------------------


def test_a_quote_inside_an_absorbed_caption_follows_its_words(chopped):
    quote = _quote(chopped, "c1", 0, len("chopped"))
    assert quote["text"] == "chopped"

    apply_cue_merge(chopped, "c0", "c2")
    assert quote["start_cue_id"] == "c0"
    assert quote["text"] == "chopped"
    joined = chopped.transcript.cue("c0")
    assert joined.text[quote["start_char_offset"] : quote["end_char_offset"]] == "chopped"


def test_a_quote_spanning_the_run_still_covers_the_same_words(chopped):
    quote = chopped.store.create(
        "tester",
        {
            "text": "sentence chopped across three",
            "start_cue_id": "c0",
            "start_char_offset": 4,
            "end_cue_id": "c2",
            "end_char_offset": 5,
        }
    )
    apply_cue_merge(chopped, "c0", "c2")
    assert (quote["start_cue_id"], quote["end_cue_id"]) == ("c0", "c0")
    assert quote["text"] == "sentence chopped across three"


def test_a_quote_after_the_run_shifts_back(chopped):
    quote = _quote(chopped, "c3", 0, len("And then"))
    apply_cue_merge(chopped, "c0", "c2")
    assert quote["start_cue_id"] == "c1"
    assert quote["text"] == "And then"


# -- handing a selected passage to another speaker -----------------------


def _hand_over(recording, cue_id, phrase, speaker="Rafael Ortiz", through=None):
    cue = recording.transcript.cue(cue_id)
    start = cue.text.index(phrase)
    end_cue = recording.transcript.cue(through or cue_id)
    return apply_selection_speaker(
        recording,
        cue_id,
        start,
        end_cue.id,
        (end_cue.text.index(phrase) + len(phrase)) if through is None else len(end_cue.text),
        speaker,
    )


def test_a_passage_mid_caption_becomes_its_own_caption(intermingled):
    """One caption into three: what came before, the passage, what came after."""
    result = _hand_over(intermingled, "c1", "Sure, I read the whole thing first.")

    assert result["splits"] == 1  # the passage runs to the end, so only one cut
    assert [cue.text for cue in intermingled.transcript.cues] == [
        "Thanks for making the time.",
        "So walk me through it.",
        "Sure, I read the whole thing first.",
        "And what breaks down there?",
    ]
    assert intermingled.transcript.cue("c2").speaker == "Rafael Ortiz"
    assert intermingled.transcript.cue("c1").speaker == "Dana Whitfield"


def test_a_passage_with_words_on_both_sides_is_cut_out(intermingled):
    result = _hand_over(intermingled, "c1", "Sure, I read")

    assert result["splits"] == 2
    assert [cue.text for cue in intermingled.transcript.cues] == [
        "Thanks for making the time.",
        "So walk me through it.",
        "Sure, I read",
        "the whole thing first.",
        "And what breaks down there?",
    ]
    assert intermingled.transcript.cue("c2").speaker == "Rafael Ortiz"
    # And the words either side stay with whoever had them.
    assert intermingled.transcript.cue("c1").speaker == "Dana Whitfield"
    assert intermingled.transcript.cue("c3").speaker == "Dana Whitfield"


def test_a_selection_covering_a_whole_caption_needs_no_cut(intermingled):
    cue = intermingled.transcript.cue("c0")
    result = apply_selection_speaker(intermingled, "c0", 0, "c0", len(cue.text), "Rafael Ortiz")

    assert result["splits"] == 0
    assert len(intermingled.transcript.cues) == 3
    assert intermingled.transcript.cue("c0").speaker == "Rafael Ortiz"


def test_a_selection_spanning_captions_takes_them_all(intermingled):
    first = intermingled.transcript.cue("c1")
    result = apply_selection_speaker(
        intermingled,
        "c1",
        first.text.index("Sure"),
        "c2",
        len(intermingled.transcript.cue("c2").text),
        "Rafael Ortiz",
    )

    assert result["splits"] == 1
    assert [cue.speaker for cue in intermingled.transcript.cues] == [
        "Dana Whitfield",
        "Dana Whitfield",
        "Rafael Ortiz",
        "Rafael Ortiz",
    ]


def test_a_selection_starting_mid_word_takes_the_whole_word(intermingled):
    """A caption boundary inside a word would leave two fragments."""
    cue = intermingled.transcript.cue("c1")
    at = cue.text.index("Sure")
    apply_selection_speaker(intermingled, "c1", at + 2, "c1", len(cue.text), "Rafael Ortiz")

    assert intermingled.transcript.cue("c2").text.startswith("Sure,")


def test_an_empty_selection_is_rejected(intermingled):
    with pytest.raises(EditError):
        apply_selection_speaker(intermingled, "c1", 5, "c1", 5, "Rafael Ortiz")


def test_handing_over_without_a_name_is_rejected(intermingled):
    cue = intermingled.transcript.cue("c1")
    with pytest.raises(EditError, match="who said it"):
        apply_selection_speaker(intermingled, "c1", cue.text.index("Sure"), "c1", len(cue.text), "  ")


def test_handing_over_backs_up_the_original_first(intermingled):
    result = _hand_over(intermingled, "c1", "Sure, I read the whole thing first.")
    assert result["backup_created"] == "meeting_original.vtt"
    assert backup_path(intermingled.folder / "meeting.vtt").read_text() == INTERMINGLED


def test_a_quote_in_the_handed_over_passage_keeps_its_words(intermingled):
    cue = intermingled.transcript.cue("c1")
    start = cue.text.index("read the whole thing")
    quote = _quote(intermingled, "c1", start, start + len("read the whole thing"))

    _hand_over(intermingled, "c1", "Sure, I read the whole thing first.")
    assert quote["text"] == "read the whole thing"
    assert quote["start_cue_id"] == "c2"
    assert quote["speaker"] == "Rafael Ortiz"


def test_reassigning_a_caption_recredits_its_quotes(one_voice):
    """A misattributed quote is the one error here that could end up published."""
    quote = _quote(one_voice, "c1", 0, 8)
    assert quote["speaker"] == "Dana Whitfield"

    result = apply_speaker_edit(one_voice, "c1", "Rafael Ortiz")
    assert [h["id"] for h in result["highlights"]] == [quote["id"]]
    assert quote["speaker"] == "Rafael Ortiz"

    # And it is on disk, not just in memory.
    reread = open_recording(one_voice.folder)
    assert reread.store.list()[0]["speaker"] == "Rafael Ortiz"


def test_quotes_in_other_captions_keep_their_speaker(one_voice):
    elsewhere = _quote(one_voice, "c3", 0, 7)
    apply_speaker_edit(one_voice, "c1", "Rafael Ortiz")
    assert elsewhere["speaker"] == "Dana Whitfield"
