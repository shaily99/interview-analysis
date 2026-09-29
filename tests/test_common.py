import json

import pytest

from subtitle_search.common import CommonSet


@pytest.fixture
def base(tmp_path):
    (tmp_path / "coders" / "ann").mkdir(parents=True)
    (tmp_path / "coders" / "ben").mkdir(parents=True)
    return tmp_path


def make(base, name="text_codebook.json"):
    return CommonSet(base, name)


def test_a_write_goes_only_to_the_writers_own_copy(base):
    common = make(base)

    common.put("ann", {"id": "k1", "name": "trust"})

    assert (base / "coders" / "ann" / "common" / "text_codebook.json").is_file()
    assert not (base / "common" / "text_codebook.json").exists()
    assert not (base / "coders" / "ben" / "common").exists()
    assert make(base).get("k1")["name"] == "trust"


def test_the_newest_version_of_a_record_wins_across_coders(base):
    make(base).put("ann", {"id": "k1", "name": "trust"})
    make(base).put("ben", {"id": "k1", "name": "reliance"})

    record = make(base).get("k1")
    assert record["name"] == "reliance"
    assert record["updated_by"] == "ben"


def test_a_deletion_is_kept_so_it_wins_over_older_copies(base):
    make(base).put("ann", {"id": "k1", "name": "trust"})
    make(base).delete("ben", "k1")

    common = make(base)
    assert common.get("k1") is None
    assert common.list() == []


def test_refresh_pushes_the_combined_records_into_the_shared_file(base):
    make(base).put("ann", {"id": "k1", "name": "trust"})
    make(base).put("ben", {"id": "k2", "name": "tone"})
    make(base).push()

    shared = json.loads((base / "common" / "text_codebook.json").read_text())["records"]
    assert sorted(shared) == ["k1", "k2"]


def test_a_record_only_in_the_shared_file_is_still_seen(base):
    (base / "common").mkdir()
    (base / "common" / "text_codebook.json").write_text(
        json.dumps({"version": 1, "records": {"k9": {"id": "k9", "name": "old", "updated_at": "2020", "updated_by": "cat"}}})
    )

    assert make(base).get("k9")["name"] == "old"


def test_conflict_copies_next_to_the_shared_file_are_reported(base):
    (base / "common").mkdir()
    (base / "common" / "text_codebook (1).json").write_text("{}")

    assert make(base).conflict_copies() == ["common/text_codebook (1).json"]


def test_an_unreadable_copy_is_skipped_and_left_alone(base):
    broken = base / "coders" / "ben" / "common" / "text_codebook.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{half synced")
    make(base).put("ann", {"id": "k1", "name": "trust"})

    assert [r["id"] for r in make(base).list()] == ["k1"]
    assert broken.read_text() == "{half synced"


# -- common codes and applications, wired into the study ----------------------

from subtitle_search.codebook import CodebookError  # noqa: E402
from subtitle_search.session import CoderBooks, open_recording  # noqa: E402

from . import fixtures  # noqa: E402


def test_common_codebooks_answer_to_the_common_coder(base):
    books = CoderBooks(base)
    code = books.text("common").add({"name": "trust", "description": "agreed"}, coder="ann")

    assert books.text("common").get(code["id"])["name"] == "trust"
    assert ("common", code["id"]) in [(c, k["id"]) for c, k in books.all_text()]
    assert "common" not in books.coders()


def test_common_code_names_are_unique_ignoring_case(base):
    books = CoderBooks(base)
    books.text("common").add({"name": "Trust"}, coder="ann")

    with pytest.raises(CodebookError):
        books.text("common").add({"name": "trust"}, coder="ben")


def test_anyone_can_edit_a_common_code(base):
    books = CoderBooks(base)
    code = books.text("common").add({"name": "trust"}, coder="ann")

    books.text("common").update(code["id"], {"name": "reliance"}, coder="ben")

    again = CoderBooks(base).text("common").get(code["id"])
    assert (again["name"], again["updated_by"]) == ("reliance", "ben")


SOURCE_QUOTE = {"id": "q1", "text": "Cool.", "start_cue_id": "c0", "start_char_offset": 0, "end_cue_id": "c0",
                "end_char_offset": 5, "start_time": 2.2, "end_time": 2.5, "color": "teal", "note": "short"}


def test_a_recording_lists_common_quotes_and_spans_for_every_mode(base):
    (base / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    recording = open_recording(base)
    recording.common_quotes.add("ann", SOURCE_QUOTE, "k1")
    recording.common_spans.add("ann", {"id": "s1", "start": 1, "end": 2, "note": ""}, "v1")

    quote = next(q for q in open_recording(base).store.list() if q["coder"] == "common")
    assert (quote["codes"], quote["notes"]) == (["k1"], [{"coder": "ann", "note": "short"}])
    span = next(s for s in open_recording(base).video_codes.list() if s["coder"] == "common")
    assert span["code_id"] == "v1"


def test_two_coders_adding_the_same_words_share_one_common_quote(base):
    (base / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    recording = open_recording(base)
    assert recording.common_quotes.add("ann", SOURCE_QUOTE, "k1") is False
    assert recording.common_quotes.add("ben", {**SOURCE_QUOTE, "id": "q9", "note": ""}, "k1") is True

    [quote] = open_recording(base).common_quotes.list()
    assert quote["contributors"] == ["ann", "ben"]


def test_the_library_shows_common_quotes_in_both_modes_marked_as_common(base):
    from subtitle_search.library import all_quotes
    from subtitle_search.session import RecordingRegistry

    (base / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    registry = RecordingRegistry()
    registry.add_folder(base)
    code = registry.books.text("common").add({"name": "trust"}, coder="ann")
    registry.default.common_quotes.add("ann", SOURCE_QUOTE, code["id"])

    assert [q["tags"] for q in all_quotes(registry, "independent", "ben")] == [["trust"]]
    assert [q["tags"] for q in all_quotes(registry, "collaborative", "ben")] == [["trust · ✓"]]
