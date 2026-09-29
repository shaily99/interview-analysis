"""Moving codes to common, and returning them to their coders."""

import pytest

from subtitle_search.codebook import CodebookError
from subtitle_search.common import ClashError, claim_returns, history_for, move_code, return_code
from subtitle_search.session import RecordingRegistry

from . import fixtures


def quote(start=0, end=5, **extra):
    return {"text": "Cool.", "start_cue_id": "c0", "start_char_offset": start, "end_cue_id": "c0", "end_char_offset": end, **extra}


@pytest.fixture
def study(tmp_path):
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    registry = RecordingRegistry()
    registry.add_folder(tmp_path)
    ann = registry.coders.create("Ann Lee", "AL")["id"]
    ben = registry.coders.create("Ben Ode", "BO")["id"]
    return registry, registry.default, ann, ben


def text_code(registry, coder, name):
    return registry.books.text(coder).add({"name": name})["id"]


def video_code(registry, coder, name):
    return registry.books.video(coder).add({"name": name})["id"]


# -- moving text codes ---------------------------------------------------------


def test_moving_a_text_code_makes_it_common_with_its_quotes(study):
    registry, rec, ann, _ = study
    trust = text_code(registry, ann, "trust")
    rec.store.create(ann, quote(codes=[trust], note="relies on it"))

    summary = move_code(registry, "text", ann, trust, final=True, description="Relies on the output")

    common = registry.books.common_text.find("trust")
    assert common["description"] == "Relies on the output"
    assert summary["moved"] == 1
    [cq] = [q for q in rec.store.list() if q["coder"] == "common"]
    assert cq["codes"] == [common["id"]]
    assert cq["notes"] == [{"coder": ann, "note": "relies on it"}]
    assert registry.books.text(ann).get(trust) is None


def test_a_quote_left_with_no_codes_is_deleted_and_other_codes_stay(study):
    registry, rec, ann, _ = study
    trust, tone = text_code(registry, ann, "trust"), text_code(registry, ann, "tone")
    only = rec.store.create(ann, quote(codes=[trust]))
    both = rec.store.create(ann, quote(start=1, codes=[trust, tone]))

    move_code(registry, "text", ann, trust, final=True, description="")

    mine = {q["id"]: q for q in rec.store.list() if q["coder"] == ann}
    assert only["id"] not in mine
    assert mine[both["id"]]["codes"] == [tone]


def test_moving_needs_the_code_confirmed_final(study):
    registry, _, ann, _ = study
    trust = text_code(registry, ann, "trust")

    with pytest.raises(CodebookError):
        move_code(registry, "text", ann, trust, final=False, description="")
    assert registry.books.text(ann).get(trust) is not None


def test_a_name_already_common_is_a_clash_unless_merged(study):
    registry, rec, ann, ben = study
    mine, theirs = text_code(registry, ann, "trust"), text_code(registry, ben, "trust")
    rec.store.create(ben, quote(codes=[theirs]))
    move_code(registry, "text", ben, theirs, final=True, description="agreed")

    with pytest.raises(ClashError) as clash:
        move_code(registry, "text", ann, mine, final=True, description="")
    assert clash.value.code["name"] == "trust"

    rec.store.create(ann, quote(codes=[mine], note="mine too"))
    summary = move_code(registry, "text", ann, mine, final=True, description="agreed, refined", into=clash.value.code["id"])
    assert summary["duplicates"] == 1
    [cq] = [q for q in rec.store.list() if q["coder"] == "common"]
    assert cq["contributors"] == sorted([ann, ben])
    assert registry.books.common_text.get(clash.value.code["id"])["description"] == "agreed, refined"


def test_a_near_duplicate_stays_a_separate_common_quote(study):
    registry, rec, ann, ben = study
    a, b = text_code(registry, ann, "trust"), text_code(registry, ben, "trust")
    rec.store.create(ann, quote(0, 5, codes=[a]))
    rec.store.create(ben, quote(1, 5, codes=[b]))
    move_code(registry, "text", ann, a, final=True, description="")
    move_code(registry, "text", ben, b, final=True, description="", into=registry.books.common_text.find("trust")["id"])

    assert len([q for q in rec.store.list() if q["coder"] == "common"]) == 2


def test_a_dry_run_counts_without_writing(study):
    registry, rec, ann, _ = study
    trust = text_code(registry, ann, "trust")
    rec.store.create(ann, quote(codes=[trust]))

    summary = move_code(registry, "text", ann, trust, final=True, description="", dry_run=True)

    assert (summary["moved"], summary["quotes_deleted"]) == (1, 1)
    assert registry.books.common_text.list() == []
    assert registry.books.text(ann).get(trust) is not None


def test_moving_twice_after_an_interruption_does_not_double_count(study):
    registry, rec, ann, _ = study
    trust = text_code(registry, ann, "trust")
    q = rec.store.create(ann, quote(codes=[trust]))
    move_code(registry, "text", ann, trust, final=True, description="")
    common_id = registry.books.common_text.find("trust")["id"]
    # As if the first move stopped before your own files were cleaned up.
    again = text_code(registry, ann, "trust")
    rec.store.create(ann, {**quote(codes=[again])})

    move_code(registry, "text", ann, again, final=True, description="", into=common_id)

    [cq] = [x for x in rec.store.list() if x["coder"] == "common"]
    assert cq["contributors"] == [ann]
    assert q["id"]


# -- moving video codes -----------------------------------------------------------


def test_video_spans_within_half_a_second_combine(study):
    registry, rec, ann, ben = study
    a, b = video_code(registry, ann, "scroll"), video_code(registry, ben, "scroll")
    rec.video_codes.add(ann, {"code_id": a, "start": 10, "end": 20}, registry.books.video(ann), 600)
    rec.video_codes.add(ben, {"code_id": b, "start": 10.4, "end": 19.6}, registry.books.video(ben), 600)
    rec.video_codes.add(ben, {"code_id": b, "start": 30, "end": 31}, registry.books.video(ben), 600)

    move_code(registry, "video", ann, a, final=True, description="")
    summary = move_code(registry, "video", ben, b, final=True, description="", into=registry.books.common_video.find("scroll")["id"])

    assert summary["duplicates"] == 1
    common = sorted((s for s in rec.video_codes.list() if s["coder"] == "common"), key=lambda s: s["start"])
    assert [(s["start"], s["contributors"]) for s in common] == [(10, sorted([ann, ben])), (30, [ben])]
    assert [s for s in rec.video_codes.list() if s["coder"] in (ann, ben)] == []


# -- returning ------------------------------------------------------------------


def test_returning_gives_each_coder_back_their_own_applications(study):
    registry, rec, ann, ben = study
    a, b = text_code(registry, ann, "trust"), text_code(registry, ben, "trust")
    rec.store.create(ann, quote(codes=[a], note="ann's"))
    rec.store.create(ben, quote(codes=[b], note="ben's"))
    move_code(registry, "text", ann, a, final=True, description="")
    common_id = registry.books.common_text.find("trust")["id"]
    move_code(registry, "text", ben, b, final=True, description="", into=common_id)
    registry.books.common_text.update(common_id, {"name": "reliance", "description": "now"}, coder=ben)

    return_code(registry, "text", ann, common_id)

    assert registry.books.common_text.get(common_id) is None
    assert [q for q in rec.store.list() if q["coder"] == "common"] == []
    # Ann asked, so hers are back at once, under the common code's current name.
    [mine] = [q for q in rec.store.list() if q["coder"] == ann]
    code = registry.books.text(ann).get(mine["codes"][0])
    assert (code["name"], code["description"], mine["note"]) == ("reliance", "now", "ann's")
    # Ben's wait until his own tool claims them.
    assert [q for q in rec.store.list() if q["coder"] == ben] == []
    claim_returns(registry, ben)
    [his] = [q for q in rec.store.list() if q["coder"] == ben]
    assert registry.books.text(ben).get(his["codes"][0])["name"] == "reliance"
    assert his["note"] == "ben's"


def test_returning_video_spans_restores_each_coders_own_times(study):
    registry, rec, ann, ben = study
    a, b = video_code(registry, ann, "scroll"), video_code(registry, ben, "scroll")
    rec.video_codes.add(ann, {"code_id": a, "start": 10, "end": 20}, registry.books.video(ann), 600)
    rec.video_codes.add(ben, {"code_id": b, "start": 10.4, "end": 19.6}, registry.books.video(ben), 600)
    move_code(registry, "video", ann, a, final=True, description="")
    common_id = registry.books.common_video.find("scroll")["id"]
    move_code(registry, "video", ben, b, final=True, description="", into=common_id)

    return_code(registry, "video", ben, common_id)
    claim_returns(registry, ann)

    spans = {s["coder"]: (s["start"], s["end"]) for s in rec.video_codes.list()}
    assert spans == {ann: (10, 20), ben: (10.4, 19.6)}


def test_history_records_moves_and_returns(study):
    registry, rec, ann, _ = study
    trust = text_code(registry, ann, "trust")
    rec.store.create(ann, quote(codes=[trust]))
    move_code(registry, "text", ann, trust, final=True, description="")
    common_id = registry.books.common_text.find("trust")["id"]
    return_code(registry, "text", ann, common_id)

    assert [e["action"] for e in history_for(registry, "text", common_id)] == ["returned", "moved"]


def test_a_caption_correction_moves_common_quotes_with_their_words(study):
    from subtitle_search.editing import apply_cue_edit

    registry, rec, ann, ben = study
    trust = text_code(registry, ann, "trust")
    cue = rec.transcript.cue("c0")
    at = cue.text.index("share my screen")
    rec.store.create(ann, {"text": "share my screen", "start_cue_id": "c0", "start_char_offset": at,
                           "end_cue_id": "c0", "end_char_offset": at + len("share my screen"), "codes": [trust]})
    move_code(registry, "text", ann, trust, final=True, description="")

    rec.store.acting = ben
    apply_cue_edit(rec, "c0", "Well. " + cue.text)

    [cq] = [q for q in rec.store.list() if q["coder"] == "common"]
    assert cq["text"] == "share my screen"
    assert cq["start_char_offset"] == at + len("Well. ")
    assert rec.common_quotes.places.get(cq["id"])["updated_by"] == ben


# -- review fixes: concurrency, clocks, returns across edits, unreadable files ---------


def test_two_coders_moving_codes_onto_the_same_words_at_once_both_survive(tmp_path):
    """Ann and Ben each move a code onto the same words before either has seen
    the other's move. Both pairings must be in common afterwards."""
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    first = RecordingRegistry()
    first.add_folder(tmp_path)
    ann = first.coders.create("Ann Lee", "AL")["id"]
    ben = first.coders.create("Ben Ode", "BO")["id"]
    a = first.books.text(ann).add({"name": "x"})["id"]
    b = first.books.text(ben).add({"name": "y"})["id"]
    first.default.store.create(ann, quote(codes=[a]))
    first.default.store.create(ben, quote(codes=[b]))

    anns_tool, bens_tool = RecordingRegistry(), RecordingRegistry()
    anns_tool.add_folder(tmp_path)
    bens_tool.add_folder(tmp_path)
    move_code(anns_tool, "text", ann, a, final=True, description="")
    move_code(bens_tool, "text", ben, b, final=True, description="")  # has not seen Ann's

    fresh = RecordingRegistry()
    fresh.add_folder(tmp_path)
    [cq] = [q for q in fresh.default.store.list() if q["coder"] == "common"]
    names = sorted(fresh.books.common_text.get(c)["name"] for c in cq["codes"])
    assert names == ["x", "y"]


def test_a_later_change_wins_even_from_a_computer_whose_clock_is_behind(study, monkeypatch):
    import subtitle_search.common as common

    registry, rec, ann, ben = study
    code = registry.books.common_text.add({"name": "trust"}, coder=ann)
    monkeypatch.setattr(common, "stamp", lambda: "2000-01-01T00:00:00.000000+00:00")

    registry.books.common_text.update(code["id"], {"name": "reliance"}, coder=ben)

    fresh = RecordingRegistry()
    fresh.add_folder(rec.folder)
    assert fresh.books.common_text.get(code["id"])["name"] == "reliance"


def test_a_return_waiting_for_a_coder_follows_a_caption_split(study):
    from subtitle_search.editing import apply_cue_split

    registry, rec, ann, ben = study
    cue = rec.transcript.cue("c2")
    b = text_code(registry, ben, "trust")
    rec.store.create(ben, {"text": cue.text[-6:], "start_cue_id": "c2", "start_char_offset": len(cue.text) - 6,
                           "end_cue_id": "c2", "end_char_offset": len(cue.text), "codes": [b]})
    move_code(registry, "text", ben, b, final=True, description="")
    return_code(registry, "text", ann, registry.books.common_text.find("trust")["id"])

    rec.store.acting = ann
    apply_cue_split(rec, "c0", 4)
    claim_returns(registry, ben)

    [his] = [q for q in rec.store.list() if q["coder"] == ben]
    # c0 became c0 and c1, so the caption the quote sat in is now c3.
    assert his["start_cue_id"] == "c3"
    assert rec.transcript.text_between(his["start_cue_id"], his["start_char_offset"], his["end_cue_id"], his["end_char_offset"]) == cue.text[-6:]


def test_a_returned_item_that_cannot_be_placed_does_not_stop_the_rest(study):
    registry, rec, ann, ben = study
    b = text_code(registry, ben, "trust")
    rec.store.create(ben, quote(codes=[b]))
    rec.store.create(ben, quote(start=1, codes=[b]))
    move_code(registry, "text", ben, b, final=True, description="")
    return_code(registry, "text", ann, registry.books.common_text.find("trust")["id"])
    broken = rec.returns.list()[0]
    rec.returns.put(ann, {**broken, "quote": {**broken["quote"], "start_cue_id": "c999", "end_cue_id": "c999"}})

    result = claim_returns(registry, ben)

    assert result == 1
    assert len([q for q in rec.store.list() if q["coder"] == ben]) == 1


def test_moving_is_refused_while_one_of_your_quote_files_cannot_be_read(study):
    registry, rec, ann, _ = study
    trust = text_code(registry, ann, "trust")
    rec.store.create(ann, quote(codes=[trust]))
    (rec.folder / "coders" / ann / "quotes.json").write_text("{half synced")
    registry.refresh()

    with pytest.raises(CodebookError):
        move_code(registry, "text", ann, trust, final=True, description="")
    assert registry.books.text(ann).get(trust) is not None
