import pytest
from fastapi.testclient import TestClient

from subtitle_search.app import FOLDER_ENV, create_app, from_environment
from subtitle_search.session import RecordingError, RecordingRegistry, open_recording

from . import fixtures

# A tiny fake media payload -- Range serving is byte plumbing and does not care
# whether the bytes decode as audio.
MEDIA_BYTES = bytes(range(256)) * 40  # 10240 bytes


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX, encoding="utf-8")
    (tmp_path / "meeting.mp4").write_bytes(MEDIA_BYTES)
    (tmp_path / "audio_only.m4a").write_bytes(b"x" * 100)
    return tmp_path


@pytest.fixture
def client(folder):
    registry = RecordingRegistry()
    registry.add_folder(folder)
    # Every write says who is making it; the default client codes as one coder.
    coder = registry.coders.create("Test Coder", "TC")
    return TestClient(create_app(registry), headers={"X-Coder": coder["id"]}), registry.default.id


def as_coder(api, name, initials):
    """A second client on the same app, logged in as another coder."""
    coder = api.post("/api/coders", json={"name": name, "initials": initials}).json()["coder"]
    return TestClient(api.app, headers={"X-Coder": coder["id"]}), coder


def test_discovery_prefers_video_over_audio(folder):
    recording = open_recording(folder)

    assert recording.media_path(0).name == "meeting.mp4"
    assert recording.media_kind == "video"
    assert recording.store.list() == []


def test_audio_only_folder(tmp_path):
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX, encoding="utf-8")
    (tmp_path / "meeting.m4a").write_bytes(b"x" * 100)

    recording = open_recording(tmp_path)
    assert recording.media_kind == "audio"


def test_folder_without_vtt_is_an_error(tmp_path):
    (tmp_path / "meeting.mp4").write_bytes(b"x")
    with pytest.raises(RecordingError):
        open_recording(tmp_path)


def test_config_and_recording_payload(client):
    api, rec_id = client

    config = api.get("/api/config").json()
    assert config["default_recording_id"] == rec_id
    assert "amber" in config["colors"]

    payload = api.get(f"/api/recordings/{rec_id}").json()
    assert payload["media_kind"] == "video"
    assert payload["transcript"]["diagnostics"]["speakers"] == ["Dana Whitfield", "Rafael Ortiz"]
    assert len(payload["transcript"]["chunks"]) == 3
    assert payload["highlights"] == []


def test_unknown_recording_is_404(client):
    api, _ = client
    assert api.get("/api/recordings/deadbeef").status_code == 404


def test_search_endpoint(client):
    api, rec_id = client
    body = api.get(f"/api/recordings/{rec_id}/search", params={"q": "share my screen"}).json()

    assert body["results"]
    assert body["results"][0]["kind"] == "exact"


def test_invalid_regex_is_a_400(client):
    api, rec_id = client
    response = api.get(
        f"/api/recordings/{rec_id}/search", params={"q": "([unclosed", "mode": "regex"}
    )
    assert response.status_code == 400


def test_highlight_crud_round_trip(client, folder):
    api, rec_id = client
    base = f"/api/recordings/{rec_id}/highlights"
    coder_id = api.headers["X-Coder"]
    ui = api.post("/api/library/text-codebook", json={"name": "ui"}).json()["code"]

    created = api.post(
        base,
        json={
            "text": "so many buttons",
            "start_cue_id": "c2",
            "start_char_offset": 9,
            "end_cue_id": "c2",
            "end_char_offset": 24,
            "color": "teal",
            "codes": [ui["id"]],
        },
    )
    assert created.status_code == 201
    highlight = created.json()["highlight"]
    assert highlight["color"] == "teal"
    assert highlight["codes"] == [ui["id"]]
    assert highlight["coder"] == coder_id

    patched = api.patch(f"{base}/{highlight['id']}", json={"note": "worth quoting"})
    assert patched.json()["highlight"]["note"] == "worth quoting"

    # One quotes file per coder, inside the recording folder.
    assert (folder / "coders" / coder_id / "quotes.json").exists()

    listing = api.get(base).json()
    assert len(listing["highlights"]) == 1

    assert api.delete(f"{base}/{highlight['id']}").status_code == 200
    assert api.get(base).json()["highlights"] == []
    assert api.delete(f"{base}/{highlight['id']}").status_code == 404


def test_highlight_with_bad_anchor_is_a_400(client):
    api, rec_id = client
    response = api.post(
        f"/api/recordings/{rec_id}/highlights",
        json={"text": "nope", "start_cue_id": "c999", "end_cue_id": "c999"},
    )
    assert response.status_code == 400


def test_media_full_request(client):
    api, rec_id = client
    response = api.get(f"/api/recordings/{rec_id}/media")

    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.content == MEDIA_BYTES


def test_media_range_returns_exact_bytes(client):
    api, rec_id = client
    response = api.get(
        f"/api/recordings/{rec_id}/media", headers={"Range": "bytes=100-199"}
    )

    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes 100-199/{len(MEDIA_BYTES)}"
    assert response.headers["content-length"] == "100"
    assert response.content == MEDIA_BYTES[100:200]


def test_media_open_ended_and_suffix_ranges(client):
    api, rec_id = client
    total = len(MEDIA_BYTES)

    open_ended = api.get(f"/api/recordings/{rec_id}/media", headers={"Range": "bytes=10000-"})
    assert open_ended.status_code == 206
    assert open_ended.content == MEDIA_BYTES[10000:]

    suffix = api.get(f"/api/recordings/{rec_id}/media", headers={"Range": "bytes=-50"})
    assert suffix.status_code == 206
    assert suffix.content == MEDIA_BYTES[total - 50:]


def test_media_unsatisfiable_range_is_416(client):
    api, rec_id = client
    response = api.get(
        f"/api/recordings/{rec_id}/media", headers={"Range": "bytes=999999-1000000"}
    )

    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(MEDIA_BYTES)}"


def test_index_is_served(client):
    api, _ = client
    response = api.get("/")
    assert response.status_code == 200
    assert "<title>" in response.text


def test_split_route_divides_a_caption_and_returns_the_session(client):
    api, rec_id = client
    text = "I can see it. Looks good on my end."

    response = api.post(
        f"/api/recordings/{rec_id}/cues/c3/split",
        json={"offset": text.index("Looks"), "text": text},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["cue_ids"] == ["c3", "c4"]

    cues = {cue["id"]: cue["text"] for cue in body["recording"]["transcript"]["cues"]}
    assert cues["c3"] == "I can see it."
    assert cues["c4"] == "Looks good on my end."
    assert cues["c5"] == "Great, thanks for confirming."


def test_split_route_rejects_a_cut_with_nothing_on_one_side(client):
    api, rec_id = client
    response = api.post(f"/api/recordings/{rec_id}/cues/c3/split", json={"offset": 0})
    assert response.status_code == 400
    assert "both sides" in response.json()["detail"]


# -- measuring word timings ---------------------------------------------


def fake_aligner(monkeypatch, per_word=0.2, gap=0.05):
    """Stand in for the acoustic model: lay each word out at a fixed rate.

    The route's job is batching, storing and reporting, none of which needs real
    audio -- and a test that downloaded half a gigabyte of weights would not run.
    """
    import subtitle_search.alignment as engine
    from subtitle_search.timings import TimedWord, split_words

    def align_cues(media_path, targets, base):
        measured = []
        for cue, first in targets:
            at = cue.start - base
            for n, (_, _, word) in enumerate(split_words(cue.text)):
                measured.append(
                    TimedWord(first + n, engine.normalize_word(word), at, at + per_word)
                )
                at += per_word + gap  # real speech leaves silence between words
        return measured

    monkeypatch.setattr(engine, "align_cues", align_cues)


def test_alignment_state_reports_availability_and_coverage(client):
    api, rec_id = client
    body = api.get(f"/api/recordings/{rec_id}/alignment").json()
    assert set(body) == {"available", "reason", "coverage"}
    assert body["coverage"] == {"timed": 0, "total": 5, "complete": False}


def test_align_measures_a_batch_and_reports_what_is_left(client, monkeypatch):
    api, rec_id = client
    fake_aligner(monkeypatch)

    first = api.post(f"/api/recordings/{rec_id}/align", json={"limit": 2}).json()
    assert first["captions"] == 2
    assert first["coverage"]["timed"] == 2
    assert first["remaining"] == 3

    rest = api.post(f"/api/recordings/{rec_id}/align", json={"limit": 100}).json()
    assert rest["coverage"] == {"timed": 5, "total": 5, "complete": True}
    assert rest["remaining"] == 0


def test_align_can_be_pointed_at_named_captions(client, monkeypatch):
    api, rec_id = client
    fake_aligner(monkeypatch)

    body = api.post(f"/api/recordings/{rec_id}/align", json={"cue_ids": ["c3"]}).json()
    assert body["coverage"]["timed"] == 1

    cues = api.get(f"/api/recordings/{rec_id}").json()["transcript"]["cues"]
    assert [c["id"] for c in cues if c["timed"]] == ["c3"]


def test_measured_times_reach_the_reader(client, monkeypatch):
    api, rec_id = client
    fake_aligner(monkeypatch)
    api.post(f"/api/recordings/{rec_id}/align", json={"cue_ids": ["c3"]})

    # c3 runs 23.1 to 27.4 and reads "I can see it. Looks good on my end." The
    # stub strides 0.25s per word, so 'Looks' -- the fifth -- starts a second in.
    # Interpolation put it at 14/35 of a 4.3s caption: nearly a second later.
    quote = api.post(
        f"/api/recordings/{rec_id}/highlights",
        json={
            "text": "Looks good",
            "start_cue_id": "c3",
            "start_char_offset": 14,
            "end_cue_id": "c3",
            "end_char_offset": 24,
        },
    ).json()["highlight"]
    assert quote["start_time"] == pytest.approx(24.1, abs=0.01)


def test_align_reports_when_it_cannot_run(client, monkeypatch):
    api, rec_id = client
    import subtitle_search.app as app_module

    monkeypatch.setattr(app_module, "alignment_available", lambda: (False, "no ffmpeg here"))
    response = api.post(f"/api/recordings/{rec_id}/align", json={})
    assert response.status_code == 503
    assert response.json()["detail"] == "no ffmpeg here"


def test_a_split_measures_the_caption_it_is_about_to_cut(client, monkeypatch):
    api, rec_id = client
    fake_aligner(monkeypatch)
    text = "I can see it. Looks good on my end."

    body = api.post(
        f"/api/recordings/{rec_id}/cues/c3/split",
        json={"offset": text.index("Looks"), "text": text},
    ).json()
    # Nothing was measured beforehand: the split asked for that one caption.
    assert body["measured"] is True
    assert body["at"] < body["tail_at"]


def test_merge_route_joins_captions_and_returns_the_session(client):
    api, rec_id = client
    body = api.post(
        f"/api/recordings/{rec_id}/cues/c0/merge", json={"through": "c1"}
    ).json()
    assert body["joined"] == 2
    assert body["cue_id"] == "c0"

    cues = {c["id"]: c["text"] for c in body["recording"]["transcript"]["cues"]}
    assert cues["c0"].startswith("Cool. And then I will share my screen")
    assert cues["c0"].endswith("So, let me share my screen.")
    assert len(cues) == 4


def test_a_split_can_be_undone_over_the_api(client):
    api, rec_id = client
    text = "I can see it. Looks good on my end."

    split = api.post(
        f"/api/recordings/{rec_id}/cues/c3/split",
        json={"offset": text.index("Looks"), "text": text, "align": False},
    ).json()
    undone = api.post(
        f"/api/recordings/{rec_id}/cues/{split['cue_ids'][0]}/merge",
        json={"through": split["cue_ids"][1], "expect": split["halves"]},
    ).json()

    cues = {c["id"]: c["text"] for c in undone["recording"]["transcript"]["cues"]}
    assert cues["c3"] == text
    assert len(cues) == 5


def test_undo_over_the_api_refuses_stale_expectations(client):
    api, rec_id = client
    response = api.post(
        f"/api/recordings/{rec_id}/cues/c0/merge",
        json={"through": "c1", "expect": ["something else", "entirely"]},
    )
    assert response.status_code == 400
    assert "changed since then" in response.json()["detail"]


# -- the reloading entry point ------------------------------------------


def test_the_app_can_be_built_from_the_environment(folder, monkeypatch):
    """What --reload imports: a fresh process has only the environment to go on."""
    monkeypatch.setenv(FOLDER_ENV, str(folder))
    api = TestClient(from_environment())

    config = api.get("/api/config").json()
    assert len(config["recordings"]) == 1
    rec_id = config["default_recording_id"]
    assert api.get(f"/api/recordings/{rec_id}").json()["transcript"]["diagnostics"][
        "cue_count"
    ] == 5


def test_building_from_an_unset_environment_says_what_to_do(monkeypatch):
    monkeypatch.delenv(FOLDER_ENV, raising=False)
    with pytest.raises(RuntimeError, match="subtitle-search <folder>"):
        from_environment()


def test_a_reload_picks_up_quotes_written_since(folder, monkeypatch):
    """Each reload re-reads the folder, so work done elsewhere is not lost."""
    monkeypatch.setenv(FOLDER_ENV, str(folder))
    first = TestClient(from_environment())
    coder = first.post("/api/coders", json={"name": "Ann", "initials": "A"}).json()["coder"]
    first.headers["X-Coder"] = coder["id"]
    rec_id = first.get("/api/config").json()["default_recording_id"]
    first.post(
        f"/api/recordings/{rec_id}/highlights",
        json={
            "text": "Cool.",
            "start_cue_id": "c0",
            "start_char_offset": 0,
            "end_cue_id": "c0",
            "end_char_offset": 5,
        },
    )

    # A second build is what a reload does: same folder, new process, new registry.
    reloaded = TestClient(from_environment())
    assert len(reloaded.get(f"/api/recordings/{rec_id}/highlights").json()["highlights"]) == 1


# -- handing a selection to another speaker ------------------------------


def test_selection_speaker_route_cuts_and_reattributes(client):
    api, rec_id = client
    # c0 is "Cool. And then I will share my screen briefly, to show you a bit of
    # a demo." -- hand the second sentence to somebody else.
    text = api.get(f"/api/recordings/{rec_id}").json()["transcript"]["cues"][0]["text"]
    body = api.post(
        f"/api/recordings/{rec_id}/selection/speaker",
        json={
            "start_cue_id": "c0",
            "start_char_offset": text.index("And then"),
            "end_cue_id": "c0",
            "end_char_offset": len(text),
            "speaker": "Jordan Reyes",
        },
    ).json()

    assert body["splits"] == 1
    assert body["speaker"] == "Jordan Reyes"
    cues = body["recording"]["transcript"]["cues"]
    assert cues[0]["text"] == "Cool."
    assert cues[0]["speaker"] == "Dana Whitfield"
    assert cues[1]["speaker"] == "Jordan Reyes"
    assert "Jordan Reyes" in body["speakers"]


def test_selection_speaker_route_needs_a_name(client):
    api, rec_id = client
    response = api.post(
        f"/api/recordings/{rec_id}/selection/speaker",
        json={
            "start_cue_id": "c0",
            "start_char_offset": 0,
            "end_cue_id": "c0",
            "end_char_offset": 5,
            "speaker": "",
        },
    )
    assert response.status_code == 400
    assert "who said it" in response.json()["detail"]


def test_selection_speaker_route_recredits_the_quotes_inside_it(client):
    api, rec_id = client
    text = api.get(f"/api/recordings/{rec_id}").json()["transcript"]["cues"][0]["text"]
    at = text.index("share my screen")
    quote = api.post(
        f"/api/recordings/{rec_id}/highlights",
        json={
            "text": "share my screen",
            "start_cue_id": "c0",
            "start_char_offset": at,
            "end_cue_id": "c0",
            "end_char_offset": at + 15,
        },
    ).json()["highlight"]
    assert quote["speaker"] == "Dana Whitfield"

    api.post(
        f"/api/recordings/{rec_id}/selection/speaker",
        json={
            "start_cue_id": "c0",
            "start_char_offset": text.index("And then"),
            "end_cue_id": "c0",
            "end_char_offset": len(text),
            "speaker": "Jordan Reyes",
        },
    )
    moved = api.get(f"/api/recordings/{rec_id}/highlights").json()["highlights"][0]
    assert moved["text"] == "share my screen"
    assert moved["speaker"] == "Jordan Reyes"


# -- video codes ---------------------------------------------------------------


def test_video_code_round_trip(client):
    http, rid = client
    code = http.post("/api/library/video-codebook", json={"name": "scroll"}).json()["code"]

    created = http.post(
        f"/api/recordings/{rid}/video-codes",
        json={"code_id": code["id"], "start": 1, "end": 3},
    )
    assert created.status_code == 201
    span = created.json()["span"]

    patched = http.patch(f"/api/recordings/{rid}/video-codes/{span['id']}", json={"end": 4})
    assert patched.json()["span"]["end"] == 4

    book = http.get("/api/library/video-codebook").json()
    assert book["codes"][0]["span_count"] == 1
    assert http.get(f"/api/recordings/{rid}").json()["video_codes"][0]["id"] == span["id"]

    assert http.delete(f"/api/recordings/{rid}/video-codes/{span['id']}").status_code == 200
    assert http.get(f"/api/recordings/{rid}/video-codes").json()["spans"] == []


def test_video_code_bad_requests(client):
    http, rid = client
    code = http.post("/api/library/video-codebook", json={"name": "scroll"}).json()["code"]

    backwards = {"code_id": code["id"], "start": 3, "end": 1}
    assert http.post(f"/api/recordings/{rid}/video-codes", json=backwards).status_code == 400
    unknown = {"code_id": "nope", "start": 1, "end": 2}
    assert http.post(f"/api/recordings/{rid}/video-codes", json=unknown).status_code == 400
    assert http.post("/api/library/video-codebook", json={"name": "Scroll"}).status_code == 400


def test_video_code_in_use_must_be_merged(client):
    http, rid = client
    a = http.post("/api/library/video-codebook", json={"name": "scroll"}).json()["code"]
    b = http.post("/api/library/video-codebook", json={"name": "swipe"}).json()["code"]
    http.post(f"/api/recordings/{rid}/video-codes", json={"code_id": a["id"], "start": 1, "end": 2})

    assert http.delete(f"/api/library/video-codebook/{a['id']}").status_code == 400

    merged = http.post(f"/api/library/video-codebook/{a['id']}/merge", json={"into": b["id"]})
    assert merged.json()["moved"] == 1
    assert [c["name"] for c in merged.json()["codes"]] == ["swipe"]
    assert http.get(f"/api/recordings/{rid}/video-codes").json()["spans"][0]["code_id"] == b["id"]


def test_video_codes_leave_text_codes_alone(client):
    http, rid = client
    http.post("/api/library/video-codebook", json={"name": "scroll"})

    assert http.get("/api/library/text-codebook").json()["codes"] == []
    assert all(t["tag"] != "scroll" for t in http.get("/api/library/vocabulary").json()["tags"])


def test_there_is_no_separate_coding_view(client):
    http, _ = client
    assert http.get("/code").status_code == 404
    assert http.get("/static/coding.js").status_code == 404
    assert 'href="/code"' not in http.get("/reader").text


# -- coders ----------------------------------------------------------------------


QUOTE = {"text": "Cool.", "start_cue_id": "c0", "start_char_offset": 0, "end_cue_id": "c0", "end_char_offset": 5}


def test_coders_can_be_listed_created_and_suggested(client):
    http, _ = client
    assert [c["initials"] for c in http.get("/api/coders").json()["coders"]] == ["TC"]
    assert http.get("/api/coders/suggest", params={"name": "Tessa Collins"}).json()["initials"] == "TCo"

    made = http.post("/api/coders", json={"name": "Rachel K.", "initials": "RK"})
    assert made.status_code == 201
    assert http.post("/api/coders", json={"name": "Rob King", "initials": "rk"}).status_code == 400


def test_a_coder_can_rename_only_themselves(client):
    http, _ = client
    other, rachel = as_coder(http, "Rachel K.", "RK")
    me = http.headers["X-Coder"]

    assert http.patch(f"/api/coders/{me}", json={"initials": "TCx"}).json()["coder"]["initials"] == "TCx"
    assert http.patch(f"/api/coders/{rachel['id']}", json={"initials": "ZZ"}).status_code == 403


def test_writes_need_a_known_coder(client):
    http, rid = client
    anonymous = TestClient(http.app)
    assert anonymous.post(f"/api/recordings/{rid}/highlights", json=QUOTE).status_code == 401
    stranger = TestClient(http.app, headers={"X-Coder": "nobody"})
    assert stranger.post(f"/api/recordings/{rid}/highlights", json=QUOTE).status_code == 401
    # Reading needs nobody.
    assert anonymous.get(f"/api/recordings/{rid}").status_code == 200


def test_another_coders_quote_is_read_only(client):
    http, rid = client
    other, _ = as_coder(http, "Rachel K.", "RK")
    mine = http.post(f"/api/recordings/{rid}/highlights", json=QUOTE).json()["highlight"]

    assert other.patch(f"/api/recordings/{rid}/highlights/{mine['id']}", json={"note": "x"}).status_code == 403
    assert other.delete(f"/api/recordings/{rid}/highlights/{mine['id']}").status_code == 403


def test_everyone_sees_every_coders_quotes_labelled(client):
    http, rid = client
    other, rachel = as_coder(http, "Rachel K.", "RK")
    http.post(f"/api/recordings/{rid}/highlights", json=QUOTE)
    other.post(f"/api/recordings/{rid}/highlights", json=QUOTE)

    coders = sorted(q["coder"] for q in http.get(f"/api/recordings/{rid}").json()["highlights"])
    assert coders == sorted([http.headers["X-Coder"], rachel["id"]])


def test_another_coders_span_is_read_only(client):
    http, rid = client
    other, _ = as_coder(http, "Rachel K.", "RK")
    code = http.post("/api/library/video-codebook", json={"name": "scroll"}).json()["code"]
    span = http.post(f"/api/recordings/{rid}/video-codes", json={"code_id": code["id"], "start": 1, "end": 2}).json()["span"]

    assert other.patch(f"/api/recordings/{rid}/video-codes/{span['id']}", json={"end": 3}).status_code == 403
    assert other.delete(f"/api/recordings/{rid}/video-codes/{span['id']}").status_code == 403
    # Nor can Rachel code with Test Coder's codes.
    assert other.post(f"/api/recordings/{rid}/video-codes", json={"code_id": code["id"], "start": 1, "end": 2}).status_code == 400


# -- the text codebook -------------------------------------------------------------


def test_text_codebook_round_trip(client):
    http, rid = client
    trust = http.post("/api/library/text-codebook", json={"name": "trust"})
    assert trust.status_code == 201
    code = trust.json()["code"]
    http.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [code["id"]]})

    listed = http.get("/api/library/text-codebook").json()["codes"]
    assert [(c["name"], c["coder"], c["quote_count"]) for c in listed] == [("trust", http.headers["X-Coder"], 1)]

    renamed = http.patch(f"/api/library/text-codebook/{code['id']}", json={"name": "reliance", "description": "x"})
    assert renamed.json()["code"]["name"] == "reliance"
    assert http.post("/api/library/text-codebook", json={"name": "Reliance"}).status_code == 400


def test_a_text_code_in_use_cannot_be_deleted_until_merged(client):
    http, rid = client
    a = http.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]
    b = http.post("/api/library/text-codebook", json={"name": "reliance"}).json()["code"]
    http.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [a["id"], b["id"]]})

    assert http.delete(f"/api/library/text-codebook/{a['id']}").status_code == 400
    merged = http.post(f"/api/library/text-codebook/{a['id']}/merge", json={"into": b["id"]}).json()
    assert merged["moved"] == 1
    assert http.get(f"/api/recordings/{rid}").json()["highlights"][0]["codes"] == [b["id"]]
    assert [c["name"] for c in merged["codes"]] == ["reliance"]


def test_another_coders_text_code_is_read_only(client):
    http, _ = client
    other, _ = as_coder(http, "Rachel K.", "RK")
    code = http.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]

    assert other.patch(f"/api/library/text-codebook/{code['id']}", json={"name": "mine"}).status_code == 403
    assert other.delete(f"/api/library/text-codebook/{code['id']}").status_code == 403
    # The same name is fine in Rachel's own codebook.
    assert other.post("/api/library/text-codebook", json={"name": "trust"}).status_code == 201


def test_library_pages_see_codes_by_name(client):
    http, rid = client
    code = http.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]
    http.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [code["id"]]})

    library = http.get("/api/library").json()
    assert [t["tag"] for t in library["tags"]] == ["trust"]
    assert http.get("/api/library/quotes").json()["quotes"][0]["tags"] == ["trust"]


# -- refresh and old files ---------------------------------------------------------


def test_refresh_shows_work_a_collaborator_synced_in(client, folder):
    http, rid = client
    elsewhere = open_recording(folder)  # the collaborator's tool, writing the same folder
    elsewhere.store.create("rk-coder", QUOTE)
    (folder / "coders" / "rk-coder" / "coder.json").write_text(
        '{"id": "rk-coder", "name": "Rachel K.", "initials": "RK"}'
    )

    assert http.get(f"/api/recordings/{rid}").json()["highlights"] == []
    assert http.post("/api/refresh").status_code == 200
    assert [q["coder"] for q in http.get(f"/api/recordings/{rid}").json()["highlights"]] == ["rk-coder"]
    assert "RK" in [c["initials"] for c in http.get("/api/coders").json()["coders"]]


def test_old_shared_files_are_reported(folder):
    (folder / "session.highlights.json").write_text("{}")
    (folder / "library.video_codebook.json").write_text("{}")
    registry = RecordingRegistry()
    registry.add_folder(folder)

    config = TestClient(create_app(registry)).get("/api/config").json()

    assert sorted(config["legacy_files"]) == ["library.video_codebook.json", "session.highlights.json"]


# -- part 1b: applications of a code, and pages that follow the mode -------------


def test_a_text_codes_applications_span_every_recording_and_can_be_taken_off(client):
    http, rid = client
    code = http.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]
    other = http.post("/api/library/text-codebook", json={"name": "tone"}).json()["code"]
    quote = http.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [code["id"], other["id"]]}).json()["highlight"]

    listed = http.get(f"/api/library/text-codebook/{code['id']}/applications").json()["applications"]
    assert [(a["ref"], a["recording_id"], a["coder"]) for a in listed] == [(f"{rid}:{quote['id']}", rid, http.headers["X-Coder"])]

    removed = http.post(f"/api/library/text-codebook/{code['id']}/applications/remove", json={"refs": [listed[0]["ref"]]})
    assert removed.json()["removed"] == 1
    kept = http.get(f"/api/recordings/{rid}").json()["highlights"]
    assert [q["codes"] for q in kept] == [[other["id"]]]  # the quote stays, without the code
    assert http.delete(f"/api/library/text-codebook/{code['id']}").status_code == 200


def test_a_video_codes_applications_can_be_removed(client):
    http, rid = client
    code = http.post("/api/library/video-codebook", json={"name": "scroll"}).json()["code"]
    span = http.post(f"/api/recordings/{rid}/video-codes", json={"code_id": code["id"], "start": 1, "end": 2}).json()["span"]

    listed = http.get(f"/api/library/video-codebook/{code['id']}/applications").json()["applications"]
    assert [a["ref"] for a in listed] == [f"{rid}:{span['id']}"]
    http.post(f"/api/library/video-codebook/{code['id']}/applications/remove", json={"refs": [listed[0]["ref"]]})
    assert http.get(f"/api/recordings/{rid}/video-codes").json()["spans"] == []


def test_another_coders_applications_cannot_be_removed(client):
    http, rid = client
    other, _ = as_coder(http, "Rachel K.", "RK")
    code = http.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]
    http.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [code["id"]]})
    ref = http.get(f"/api/library/text-codebook/{code['id']}/applications").json()["applications"][0]["ref"]

    response = other.post(f"/api/library/text-codebook/{code['id']}/applications/remove", json={"refs": [ref]})
    assert response.status_code == 403


def test_library_follows_the_mode(client):
    http, rid = client
    other, _ = as_coder(http, "Rachel K.", "RK")
    mine = http.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]
    theirs = other.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]
    http.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [mine["id"]]})
    other.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [theirs["id"]]})

    alone = http.get("/api/library", params={"mode": "independent"}).json()
    assert [t["tag"] for t in alone["tags"]] == ["trust"]
    assert alone["quote_count"] == 1
    assert len(http.get("/api/library/quotes", params={"mode": "independent"}).json()["quotes"]) == 1

    together = http.get("/api/library", params={"mode": "collaborative"}).json()
    assert sorted(t["tag"] for t in together["tags"]) == ["trust · RK", "trust · TC"]
    assert together["quote_count"] == 2


def test_the_library_lists_video_codes_per_recording(client):
    http, rid = client
    code = http.post("/api/library/video-codebook", json={"name": "scroll"}).json()["code"]
    http.post(f"/api/recordings/{rid}/video-codes", json={"code_id": code["id"], "start": 1, "end": 2})

    video = http.get("/api/library", params={"mode": "independent"}).json()["video"]
    assert [(v["name"], v["span_count"], v["recordings"]) for v in video] == [("scroll", 1, {rid: 1})]


def test_the_codebook_page_is_served(client):
    http, _ = client
    page = http.get("/codebook")
    assert page.status_code == 200
    assert "codebook.js" in page.text
    assert page.headers["cache-control"] == "no-cache"


# -- part 2: common codes -----------------------------------------------------------


def test_moving_a_code_to_common_over_the_api(client):
    http, rid = client
    code = http.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]
    http.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [code["id"]], "note": "n"})
    base = f"/api/library/text-codebook/{code['id']}/move"

    assert http.post(base, json={"final": False}).status_code == 400
    preview = http.post(base, params={"dry_run": 1}, json={"final": True}).json()
    assert (preview["moved"], preview["quotes_deleted"]) == (1, 1)

    moved = http.post(base, json={"final": True, "description": "agreed"})
    assert moved.status_code == 200
    codes = http.get("/api/library/text-codebook").json()["codes"]
    assert [(c["name"], c["coder"], c["quote_count"]) for c in codes] == [("trust", "common", 1)]
    [quote] = http.get(f"/api/recordings/{rid}").json()["highlights"]
    assert quote["coder"] == "common"


def test_a_clash_is_a_409_naming_the_common_code(client):
    http, rid = client
    other, _ = as_coder(http, "Rachel K.", "RK")
    theirs = other.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]
    other.post(f"/api/library/text-codebook/{theirs['id']}/move", json={"final": True})
    mine = http.post("/api/library/text-codebook", json={"name": "Trust"}).json()["code"]

    clash = http.post(f"/api/library/text-codebook/{mine['id']}/move", json={"final": True})
    assert clash.status_code == 409
    common_id = clash.json()["code"]["id"]
    merged = http.post(f"/api/library/text-codebook/{mine['id']}/move", json={"final": True, "into": common_id})
    assert merged.status_code == 200


def test_anyone_may_edit_return_and_read_the_history_of_a_common_code(client):
    http, rid = client
    other, _ = as_coder(http, "Rachel K.", "RK")
    code = http.post("/api/library/video-codebook", json={"name": "scroll"}).json()["code"]
    http.post(f"/api/recordings/{rid}/video-codes", json={"code_id": code["id"], "start": 1, "end": 2})
    http.post(f"/api/library/video-codebook/{code['id']}/move", json={"final": True})
    common_id = next(c["id"] for c in http.get("/api/library/video-codebook").json()["codes"] if c["coder"] == "common")

    assert other.patch(f"/api/library/video-codebook/{common_id}", json={"description": "moving through results"}).status_code == 200
    assert other.delete(f"/api/library/video-codebook/{common_id}").status_code == 400  # still has a span
    assert other.post(f"/api/library/video-codebook/{common_id}/return").status_code == 200

    history = http.get(f"/api/library/video-codebook/{common_id}/history").json()["entries"]
    assert [e["action"] for e in history] == ["returned", "edited", "moved"]
    # Test Coder's span waits for them; Refresh claims it.
    assert [s["coder"] for s in http.get(f"/api/recordings/{rid}/video-codes").json()["spans"]] == []
    http.post("/api/refresh")
    assert [s["coder"] for s in http.get(f"/api/recordings/{rid}/video-codes").json()["spans"]] == [http.headers["X-Coder"]]


def test_refresh_pushes_and_updates_the_last_synced_time(client, folder):
    http, _ = client
    code = http.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]
    http.post(f"/api/library/text-codebook/{code['id']}/move", json={"final": True})

    before = http.get("/api/common/status").json()
    assert "pending" not in before
    http.post("/api/refresh")
    after = http.get("/api/common/status").json()
    assert after["synced_at"] > before["synced_at"]
    assert (folder / "common" / "text_codebook.json").is_file()


def test_removing_a_common_codes_quotes_from_the_code_page(client):
    http, rid = client
    code = http.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]
    http.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [code["id"]]})
    http.post(f"/api/library/text-codebook/{code['id']}/move", json={"final": True})
    common_id = http.get("/api/library/text-codebook").json()["codes"][0]["id"]
    other, _ = as_coder(http, "Rachel K.", "RK")

    ref = other.get(f"/api/library/text-codebook/{common_id}/applications").json()["applications"][0]["ref"]
    assert other.post(f"/api/library/text-codebook/{common_id}/applications/remove", json={"refs": [ref]}).json()["removed"] == 1
    assert http.get(f"/api/recordings/{rid}").json()["highlights"] == []
    assert other.delete(f"/api/library/text-codebook/{common_id}").status_code == 200


def test_transcript_corrections_need_a_known_coder(client):
    http, rid = client
    anonymous = TestClient(http.app)
    assert anonymous.patch(f"/api/recordings/{rid}/cues/c0", json={"text": "Changed."}).status_code == 401
    assert http.patch(f"/api/recordings/{rid}/cues/c0", json={"text": "Changed."}).status_code == 200


def test_the_reader_is_three_panes_with_code_rows_and_a_mute_button(client):
    http, _ = client
    reader = http.get("/reader").text
    for marker in ('id="panes"', 'data-pane="video"', 'data-pane="vcodes"', 'data-pane="transcript"',
                   'data-pane="codes"', 'id="search-drop"', 'id="vc-tiers"', 'id="mute"'):
        assert marker in reader
    assert http.get("/static/layout.js").status_code == 200


def test_similar_quotes_follow_the_mode(client):
    http, rid = client
    other, _ = as_coder(http, "Rachel K.", "RK")
    mine = http.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]
    theirs = other.post("/api/library/text-codebook", json={"name": "trust"}).json()["code"]
    quote = http.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [mine["id"]]}).json()["highlight"]
    other.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [theirs["id"]]})

    ref = f"{rid}:{quote['id']}"
    alone = http.get("/api/library/similar", params={"ref": ref, "mode": "independent"}).json()["similar"]
    together = http.get("/api/library/similar", params={"ref": ref, "mode": "collaborative"}).json()["similar"]

    assert alone == []
    assert len(together) == 1
