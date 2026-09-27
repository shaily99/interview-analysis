import json

import pytest

from subtitle_search.video_codes import (
    VideoCodebook,
    VideoCodeError,
    VideoCodeStore,
)


@pytest.fixture
def codebook(tmp_path):
    return VideoCodebook(tmp_path / "library.video_codebook.json")


@pytest.fixture
def spans(tmp_path):
    return VideoCodeStore(tmp_path / "session.video_codes.json")


def test_codes_persist_and_reload(tmp_path, codebook):
    code = codebook.add({"name": "scroll", "key": "q"})

    reloaded = VideoCodebook(codebook.path)

    assert reloaded.get(code["id"])["name"] == "scroll"
    assert reloaded.get(code["id"])["key"] == "q"
    assert code["color"]


def test_names_are_unique_ignoring_case(codebook):
    codebook.add({"name": "Scroll"})

    with pytest.raises(VideoCodeError):
        codebook.add({"name": "scroll"})


def test_rename_keeps_the_id(codebook):
    code = codebook.add({"name": "scroll"})

    codebook.update(code["id"], {"name": "scrolling", "color": "green"})

    assert codebook.get(code["id"])["name"] == "scrolling"
    assert codebook.get(code["id"])["color"] == "green"


@pytest.mark.parametrize("key", ["j", "i", "1", "qq"])
def test_reader_keys_cannot_be_code_keys(codebook, key):
    with pytest.raises(VideoCodeError):
        codebook.add({"name": "scroll", "key": key})


def test_two_codes_cannot_share_a_key(codebook):
    codebook.add({"name": "scroll", "key": "q"})

    with pytest.raises(VideoCodeError):
        codebook.add({"name": "click", "key": "q"})


def test_span_round_trip(spans, codebook):
    code = codebook.add({"name": "scroll"})

    span = spans.add({"code_id": code["id"], "start": 62, "end": 90.5}, codebook, 600)

    assert VideoCodeStore(spans.path).list() == [span]
    assert (span["start"], span["end"]) == (62.0, 90.5)


@pytest.mark.parametrize("start,end", [(10, 10), (10, 5), (-1, 4), (590, 700), ("x", 3)])
def test_bad_times_are_refused(spans, codebook, start, end):
    code = codebook.add({"name": "scroll"})

    with pytest.raises(VideoCodeError):
        spans.add({"code_id": code["id"], "start": start, "end": end}, codebook, 600)


def test_unknown_code_is_refused(spans, codebook):
    with pytest.raises(VideoCodeError):
        spans.add({"code_id": "nope", "start": 1, "end": 2}, codebook, 600)


def test_bad_update_changes_nothing(spans, codebook):
    code = codebook.add({"name": "scroll"})
    span = spans.add({"code_id": code["id"], "start": 1, "end": 2}, codebook, 600)

    with pytest.raises(VideoCodeError):
        spans.update(span["id"], {"start": 5, "note": "lost"}, codebook, 600)

    assert spans.list()[0]["start"] == 1
    assert spans.list()[0]["note"] == ""


def test_unknown_fields_survive_an_update(spans, codebook):
    code = codebook.add({"name": "scroll"})
    span = spans.add({"code_id": code["id"], "start": 1, "end": 2}, codebook, 600)
    data = json.loads(spans.path.read_text())
    data["spans"][0]["from_the_future"] = True
    spans.path.write_text(json.dumps(data))

    reloaded = VideoCodeStore(spans.path)
    reloaded.update(span["id"], {"note": "fast"}, codebook, 600)

    assert VideoCodeStore(spans.path).list()[0]["from_the_future"] is True


def test_corrupt_file_is_kept_aside(tmp_path):
    path = tmp_path / "session.video_codes.json"
    path.write_text("{not json")

    store = VideoCodeStore(path)

    assert store.list() == []
    assert (tmp_path / "session.video_codes.json.corrupt").read_text() == "{not json"


def test_no_temp_files_left_behind(tmp_path, spans, codebook):
    code = codebook.add({"name": "scroll"})
    spans.add({"code_id": code["id"], "start": 1, "end": 2}, codebook, 600)

    assert not list(tmp_path.glob("*.tmp"))


def test_reassign_moves_spans(spans, codebook):
    a = codebook.add({"name": "scroll"})
    b = codebook.add({"name": "swipe"})
    spans.add({"code_id": a["id"], "start": 1, "end": 2}, codebook, 600)
    spans.add({"code_id": b["id"], "start": 3, "end": 4}, codebook, 600)

    assert spans.reassign(a["id"], b["id"]) == 1
    assert spans.count(b["id"]) == 2
