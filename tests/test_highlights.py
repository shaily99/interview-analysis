import json

import pytest

from subtitle_search.codebook import TextCodebook
from subtitle_search.highlights import HighlightError, HighlightStore, SessionQuotes
from subtitle_search.vtt import parse_vtt

from . import fixtures


@pytest.fixture
def book(tmp_path):
    codebook = TextCodebook(tmp_path / "book" / "text_codebook.json")
    for name in ("demo", "intro", "key"):
        codebook.add({"name": name})
    return codebook


def code(book, name):
    return book.find(name)["id"]


@pytest.fixture
def store(tmp_path, book):
    transcript = parse_vtt(fixtures.COLON_PREFIX, source_name="talk.vtt")
    return HighlightStore(tmp_path / "talk.highlights.json", transcript, book)


def _payload(**overrides):
    base = {
        "text": "share my screen briefly",
        "start_cue_id": "c0",
        "start_char_offset": 27,
        "end_cue_id": "c0",
        "end_char_offset": 50,
        "color": "amber",
        "note": "good framing",
    }
    base.update(overrides)
    return base


def test_create_resolves_times_from_cue_anchors(store):
    highlight = store.create(_payload())
    cue = store.transcript.cue("c0")

    assert cue.start <= highlight["start_time"] <= highlight["end_time"] <= cue.end
    # Anchored partway into a 10s cue, so it should not collapse to the cue start.
    assert highlight["start_time"] > cue.start
    assert highlight["speaker"] == "Dana Whitfield"


def test_create_persists_to_disk_immediately(store):
    store.create(_payload())

    data = json.loads(store.path.read_text())
    assert len(data["highlights"]) == 1
    assert data["highlights"][0]["note"] == "good framing"
    assert data["vtt_file"] == "talk.vtt"
    assert data["vtt_sha256"] == store.transcript.sha256


def test_selection_spanning_cues_gets_a_range(store):
    highlight = store.create(
        _payload(start_cue_id="c0", start_char_offset=10, end_cue_id="c2", end_char_offset=20)
    )

    assert highlight["start_time"] < highlight["end_time"]
    assert highlight["end_time"] > store.transcript.cue("c1").start


def test_reversed_selection_is_normalized(store):
    """Selecting right-to-left must not produce an inverted time range."""
    highlight = store.create(
        _payload(start_cue_id="c2", start_char_offset=5, end_cue_id="c0", end_char_offset=5)
    )

    assert highlight["start_cue_id"] == "c0"
    assert highlight["end_cue_id"] == "c2"
    assert highlight["start_time"] <= highlight["end_time"]


def test_empty_selection_is_rejected(store):
    with pytest.raises(HighlightError):
        store.create(_payload(text="   "))


def test_unknown_cue_is_rejected(store):
    with pytest.raises(HighlightError):
        store.create(_payload(start_cue_id="c999"))


def test_update_and_delete(store, book):
    highlight = store.create(_payload())

    updated = store.update(highlight["id"], {"note": "revised", "color": "teal", "codes": [code(book, "key")]})
    assert updated["note"] == "revised"
    assert updated["color"] == "teal"
    assert updated["codes"] == [code(book, "key")]

    assert store.delete(highlight["id"]) is True
    assert store.list() == []
    assert store.delete(highlight["id"]) is False


def test_invalid_color_falls_back_to_default(store):
    highlight = store.create(_payload(color="chartreuse"))
    assert highlight["color"] == "amber"


def test_a_quote_carries_text_codes_by_id(store, book):
    highlight = store.create(_payload(codes=[code(book, "demo"), code(book, "intro"), code(book, "demo")]))

    assert highlight["codes"] == [code(book, "demo"), code(book, "intro")]
    assert "tags" not in highlight


def test_a_code_from_outside_the_codebook_is_refused(store):
    with pytest.raises(HighlightError):
        store.create(_payload(codes=["not-a-code"]))


def test_a_quote_can_be_saved_with_no_codes(store):
    assert store.create(_payload())["codes"] == []


def test_unknown_fields_survive_a_round_trip(tmp_path):
    """A file written by a later version must not be silently stripped."""
    transcript = parse_vtt(fixtures.COLON_PREFIX, source_name="talk.vtt")
    path = tmp_path / "talk.highlights.json"
    path.write_text(
        json.dumps(
            {
                "version": 99,
                "vtt_sha256": transcript.sha256,
                "future_top_level_field": {"keep": "me"},
                "known_tags": [],
                "highlights": [
                    {
                        "id": "abc123",
                        "text": "an existing quote",
                        "start_cue_id": "c0",
                        "end_cue_id": "c0",
                        "start_char_offset": 0,
                        "end_char_offset": 10,
                        "start_time": 2.18,
                        "end_time": 3.0,
                        "future_field": "preserved",
                    }
                ],
            }
        )
    )

    store = HighlightStore(path, transcript)
    store.update("abc123", {"note": "added later"})

    data = json.loads(path.read_text())
    assert data["future_top_level_field"] == {"keep": "me"}
    assert data["highlights"][0]["future_field"] == "preserved"
    assert data["highlights"][0]["note"] == "added later"


def test_stale_detection_when_transcript_changed(tmp_path):
    transcript = parse_vtt(fixtures.COLON_PREFIX, source_name="talk.vtt")
    path = tmp_path / "talk.highlights.json"

    store = HighlightStore(path, transcript)
    store.create(_payload())
    assert store.stale is False

    other = parse_vtt(fixtures.VOICE_TAG, source_name="talk.vtt")
    assert HighlightStore(path, other).stale is True


def test_corrupt_file_is_preserved_not_overwritten(tmp_path):
    transcript = parse_vtt(fixtures.COLON_PREFIX, source_name="talk.vtt")
    path = tmp_path / "talk.highlights.json"
    path.write_text("{not valid json at all")

    store = HighlightStore(path, transcript)
    store.create(_payload())

    assert (tmp_path / "talk.highlights.json.corrupt").read_text() == "{not valid json at all"
    assert len(store.list()) == 1


def test_write_leaves_no_temp_files_behind(store, tmp_path):
    store.create(_payload())
    store.create(_payload(text="another quote"))

    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["talk.highlights.json"]


# -- several coders in one recording -------------------------------------------


@pytest.fixture
def quotes(tmp_path):
    transcript = parse_vtt(fixtures.COLON_PREFIX, source_name="talk.vtt")
    stores = {
        coder: HighlightStore(tmp_path / coder / "quotes.json", transcript, TextCodebook(tmp_path / coder / "book.json"))
        for coder in ("ann", "ben")
    }
    return SessionQuotes(stores)


def test_every_quote_is_labelled_with_its_coder(quotes):
    quotes.create("ann", _payload())
    quotes.create("ben", _payload(text="another"))

    assert sorted(q["coder"] for q in quotes.list()) == ["ann", "ben"]


def test_each_coder_writes_only_their_own_file(tmp_path, quotes):
    quotes.create("ann", _payload())

    assert (tmp_path / "ann" / "quotes.json").is_file()
    assert not (tmp_path / "ben" / "quotes.json").exists()


def test_the_coder_label_is_not_written_into_the_file(tmp_path, quotes):
    quotes.create("ann", _payload())

    assert "coder" not in json.loads((tmp_path / "ann" / "quotes.json").read_text())["highlights"][0]


def test_another_coders_quote_cannot_be_changed(quotes):
    mine = quotes.create("ann", _payload())

    with pytest.raises(PermissionError):
        quotes.update("ben", mine["id"], {"note": "mine now"})
    with pytest.raises(PermissionError):
        quotes.delete("ben", mine["id"])
    assert quotes.list()[0]["note"] == "good framing"


def test_a_coder_can_change_and_delete_their_own_quote(quotes):
    mine = quotes.create("ann", _payload())

    assert quotes.update("ann", mine["id"], {"note": "revised"})["note"] == "revised"
    assert quotes.delete("ann", mine["id"]) is True
    assert quotes.list() == []


def test_an_unknown_quote_is_a_key_error(quotes):
    with pytest.raises(KeyError):
        quotes.update("ann", "nope", {"note": "x"})


def test_a_coder_without_a_store_yet_gets_one_on_first_quote(tmp_path):
    transcript = parse_vtt(fixtures.COLON_PREFIX, source_name="talk.vtt")
    made = []

    def factory(coder):
        made.append(coder)
        return HighlightStore(tmp_path / coder / "quotes.json", transcript)

    quotes = SessionQuotes({}, factory=factory)
    quotes.create("cara", _payload())

    assert made == ["cara"]
    assert quotes.list()[0]["coder"] == "cara"


def test_speaker_changes_reach_every_coders_quotes(quotes):
    quotes.create("ann", _payload())
    quotes.create("ben", _payload(text="another"))

    touched = quotes.restate_speaker(["c0"], "Someone Else")

    assert sorted(q["coder"] for q in touched) == ["ann", "ben"]
    assert {q["speaker"] for q in quotes.list()} == {"Someone Else"}


def test_merging_one_text_code_into_another_keeps_one_of_each(store, book):
    demo, intro = code(book, "demo"), code(book, "intro")
    store.create(_payload(codes=[demo, intro]))
    store.create(_payload(text="second", codes=[demo]))

    assert store.count_code(demo) == 2
    assert store.reassign_code(demo, intro) == 2
    assert [q["codes"] for q in store.list()] == [[intro], [intro]]
