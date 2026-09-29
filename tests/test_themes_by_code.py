"""Themes built from codes: per-coder theme stores, common themes, and repointing."""

import json

import pytest

from subtitle_search.library import CommonThemeStore, ThemeStore


@pytest.fixture
def store(tmp_path):
    return ThemeStore(tmp_path / "coders" / "ann" / "themes.json")


def test_renaming_a_code_in_themes_keeps_one_card_per_theme(store):
    theme = store.create("Trust")
    store.place("text:a", theme["id"], 40, 120)
    store.place("text:b", theme["id"], 40, 200)

    store.replace_ref("text:a", "text:b")

    assert [c["ref"] for c in store.cards()] == ["text:b"]
    assert store.list()[0]["refs"] == ["text:b"]


def test_removing_a_code_takes_it_out_of_every_theme(store):
    first, second = store.create("One"), store.create("Two")
    store.place("video:v", first["id"], 40, 120)
    store.place("video:v", second["id"], 40, 120)

    store.remove_ref("video:v")

    assert store.cards() == []
    assert [t["refs"] for t in store.list()] == [[], []]


def test_reading_another_coders_unreadable_themes_leaves_the_file(tmp_path):
    path = tmp_path / "coders" / "ben" / "themes.json"
    path.parent.mkdir(parents=True)
    path.write_text("{half synced")

    assert ThemeStore(path).list() == []
    assert path.read_text() == "{half synced"


def make_common(root, writer):
    store = CommonThemeStore(root)
    store.writer = writer
    return store


def test_two_coders_editing_common_themes_at_once_keep_both_changes(tmp_path):
    (tmp_path / "coders" / "ann").mkdir(parents=True)
    (tmp_path / "coders" / "ben").mkdir(parents=True)
    theme = make_common(tmp_path, "ann").create("Trust", box={"x": 0, "y": 0, "w": 600, "h": 400})
    anns, bens = make_common(tmp_path, "ann"), make_common(tmp_path, "ben")

    anns.place("text:a", theme["id"], 40, 120)
    bens.place("text:b", theme["id"], 40, 200)  # has not seen Ann's card

    fresh = make_common(tmp_path, "cat")
    assert sorted(c["ref"] for c in fresh.cards()) == ["text:a", "text:b"]
    assert sorted(fresh.list()[0]["refs"]) == ["text:a", "text:b"]


def test_common_theme_changes_are_written_to_the_writers_own_copy(tmp_path):
    (tmp_path / "coders" / "ann").mkdir(parents=True)
    make_common(tmp_path, "ann").create("Trust")

    assert (tmp_path / "coders" / "ann" / "common" / "themes.json").is_file()
    assert not (tmp_path / "common" / "themes.json").exists()


def test_deleting_a_common_theme_removes_its_cards_for_everyone(tmp_path):
    (tmp_path / "coders" / "ann").mkdir(parents=True)
    store = make_common(tmp_path, "ann")
    theme = store.create("Trust", box={"x": 0, "y": 0, "w": 600, "h": 400})
    store.place("text:a", theme["id"], 40, 120)

    make_common(tmp_path, "ben").delete(theme["id"])

    fresh = make_common(tmp_path, "cat")
    assert fresh.list() == [] and fresh.cards() == []


# -- over the API ---------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from subtitle_search.app import create_app  # noqa: E402
from subtitle_search.session import RecordingRegistry  # noqa: E402

from . import fixtures  # noqa: E402

QUOTE = {"text": "Cool.", "start_cue_id": "c0", "start_char_offset": 0, "end_cue_id": "c0", "end_char_offset": 5}
BOX = {"x": 0, "y": 0, "w": 600, "h": 400}


@pytest.fixture
def api(tmp_path):
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    registry = RecordingRegistry()
    registry.add_folder(tmp_path)
    ann = registry.coders.create("Ann Lee", "AL")["id"]
    ben = registry.coders.create("Ben Ode", "BO")["id"]
    app = create_app(registry)
    return (TestClient(app, headers={"X-Coder": ann}), TestClient(app, headers={"X-Coder": ben}),
            registry.default.id, tmp_path)


def text_code(client, rid, name, quote=True):
    code = client.post("/api/library/text-codebook", json={"name": name}).json()["code"]
    if quote:
        client.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [code["id"]]})
    return code


def test_the_page_gets_codes_shaped_like_cards(api):
    ann, ben, rid, _ = api
    trust = text_code(ann, rid, "trust")
    text_code(ben, rid, "tone")

    mine = ann.get("/api/library/codes", params={"mode": "independent"}).json()["codes"]
    assert [(c["ref"], c["text"], c["count"], c["recordings"]) for c in mine] == [(f"text:{trust['id']}", "trust", 1, [rid])]
    assert mine[0]["applications"][0]["recording_id"] == rid
    everyone = ann.get("/api/library/codes", params={"mode": "collaborative"}).json()["codes"]
    assert sorted(c["text"] for c in everyone) == ["tone · BO", "trust · AL"]


def test_each_coder_has_their_own_themes_and_others_are_read_only(api):
    ann, ben, rid, root = api
    trust = text_code(ann, rid, "trust")
    theme = ann.post("/api/library/themes", json={"title": "Reliance", "box": BOX}).json()["theme"]
    ann.post("/api/library/canvas/place", json={"ref": f"text:{trust['id']}", "theme_id": theme["id"], "x": 40, "y": 120})

    assert (root / "coders" / ann.headers["X-Coder"] / "themes.json").is_file()
    assert ben.get("/api/library/themes", params={"mode": "independent"}).json()["themes"] == []
    seen = ben.get("/api/library/themes", params={"mode": "collaborative"}).json()["themes"]
    assert [(t["title"], t["coder"]) for t in seen] == [("Reliance", ann.headers["X-Coder"])]
    assert ben.patch(f"/api/library/themes/{theme['id']}", json={"title": "Mine"}).status_code == 403


def test_a_theme_takes_only_your_own_and_common_codes(api):
    ann, ben, rid, _ = api
    theirs = text_code(ben, rid, "tone")
    theme = ann.post("/api/library/themes", json={"title": "T", "box": BOX}).json()["theme"]

    placed = ann.post("/api/library/canvas/place", json={"ref": f"text:{theirs['id']}", "theme_id": theme["id"], "x": 40, "y": 120})
    assert placed.status_code == 400
    assert ann.post("/api/library/canvas/place", json={"ref": "rec:q1", "theme_id": theme["id"], "x": 40, "y": 120}).status_code == 400


def test_promoting_a_theme_waits_until_its_codes_are_common(api):
    ann, _, rid, _ = api
    trust = text_code(ann, rid, "trust")
    theme = ann.post("/api/library/themes", json={"title": "Reliance", "box": BOX}).json()["theme"]
    ann.post("/api/library/canvas/place", json={"ref": f"text:{trust['id']}", "theme_id": theme["id"], "x": 40, "y": 120})

    blocked = ann.post(f"/api/library/themes/{theme['id']}/move", json={"final": True})
    assert blocked.status_code == 409
    assert [b["name"] for b in blocked.json()["blocking"]] == ["trust"]

    # Moving the code repoints the theme at the common code, which unblocks it.
    ann.post(f"/api/library/text-codebook/{trust['id']}/move", json={"final": True})
    common_ref = next(c["ref"] for c in ann.get("/api/library/codes", params={"mode": "independent"}).json()["codes"] if c["coder"] == "common")
    assert ann.get("/api/library/themes", params={"mode": "independent"}).json()["themes"][0]["refs"] == [common_ref]

    assert ann.post(f"/api/library/themes/{theme['id']}/move", json={"final": False}).status_code == 400
    moved = ann.post(f"/api/library/themes/{theme['id']}/move", json={"final": True, "description": "Relying on output"})
    assert moved.status_code == 200
    [common] = ann.get("/api/library/themes", params={"mode": "independent"}).json()["themes"]
    assert (common["coder"], common["note"], common["refs"]) == ("common", "Relying on output", [common_ref])


def test_a_common_theme_takes_only_common_codes(api):
    ann, ben, rid, _ = api
    trust = text_code(ann, rid, "trust")
    ann.post(f"/api/library/text-codebook/{trust['id']}/move", json={"final": True})
    theme = ann.post("/api/library/themes", json={"title": "Reliance", "box": BOX}).json()["theme"]
    ann.post(f"/api/library/themes/{theme['id']}/move", json={"final": True})
    common_theme = ann.get("/api/library/themes", params={"mode": "independent"}).json()["themes"][0]
    own = text_code(ben, rid, "hesitation")

    refused = ben.post("/api/library/canvas/place", json={"ref": f"text:{own['id']}", "theme_id": common_theme["id"], "x": 40, "y": 120})
    assert refused.status_code == 400
    common_ref = next(c["ref"] for c in ben.get("/api/library/codes").json()["codes"] if c["coder"] == "common")
    ok = ben.post("/api/library/canvas/place", json={"ref": common_ref, "theme_id": common_theme["id"], "x": 40, "y": 120})
    assert ok.status_code == 200


def test_deleting_a_code_takes_it_out_of_your_themes(api):
    ann, _, rid, _ = api
    tone = text_code(ann, rid, "tone", quote=False)
    theme = ann.post("/api/library/themes", json={"title": "T", "box": BOX}).json()["theme"]
    ann.post("/api/library/canvas/place", json={"ref": f"text:{tone['id']}", "theme_id": theme["id"], "x": 40, "y": 120})

    ann.delete(f"/api/library/text-codebook/{tone['id']}")

    assert ann.get("/api/library/themes").json()["themes"][0]["refs"] == []


def test_returning_a_common_code_takes_it_out_of_common_themes(api):
    ann, _, rid, _ = api
    trust = text_code(ann, rid, "trust")
    ann.post(f"/api/library/text-codebook/{trust['id']}/move", json={"final": True})
    common = next(c for c in ann.get("/api/library/codes").json()["codes"] if c["coder"] == "common")
    theme = ann.post("/api/library/themes", json={"title": "T", "box": BOX}).json()["theme"]
    ann.post("/api/library/canvas/place", json={"ref": common["ref"], "theme_id": theme["id"], "x": 40, "y": 120})
    ann.post(f"/api/library/themes/{theme['id']}/move", json={"final": True})

    ann.post(f"/api/library/text-codebook/{common['id']}/return")

    [common_theme] = ann.get("/api/library/themes").json()["themes"]
    assert common_theme["refs"] == []


def test_a_lassoed_theme_is_made_from_the_quotes_codes(api):
    ann, _, rid, _ = api
    trust = text_code(ann, rid, "trust")
    quote_ref = ann.get("/api/library/quotes").json()["quotes"][0]["ref"]

    body = ann.post("/api/library/themes/from-refs", json={"title": "Lasso", "refs": [quote_ref]}).json()

    assert body["theme"]["refs"] == [f"text:{trust['id']}"]


def test_tidying_orders_code_cards_alphabetically(api):
    ann, _, rid, _ = api
    names = ["zeal", "anger", "mood"]
    refs = [f"text:{text_code(ann, rid, n, quote=False)['id']}" for n in names]
    theme = ann.post("/api/library/themes", json={"title": "T", "box": BOX}).json()["theme"]
    for i, ref in enumerate(refs):
        ann.post("/api/library/canvas/place", json={"ref": ref, "theme_id": theme["id"], "x": 40, "y": 120 + 60 * i})

    body = ann.post("/api/library/canvas/tidy", json={"theme_id": theme["id"]}).json()

    by_ref = dict(zip(refs, names))
    packed = sorted(body["cards"], key=lambda c: (c["y"], c["x"]))
    assert [by_ref[c["ref"]] for c in packed] == ["anger", "mood", "zeal"]


def test_the_old_shared_themes_file_is_reported(tmp_path):
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    (tmp_path / "library.themes.json").write_text("{}")
    registry = RecordingRegistry()
    registry.add_folder(tmp_path)

    assert "library.themes.json" in TestClient(create_app(registry)).get("/api/config").json()["legacy_files"]


# -- review fixes ----------------------------------------------------------------


def test_reading_another_coders_themes_never_rewrites_them(api):
    ann, ben, rid, root = api
    trust = text_code(ann, rid, "trust")
    ann.post("/api/library/themes", json={"title": "T", "box": BOX,
                                          "cards": [{"ref": f"text:{trust['id']}", "x": 5000, "y": 5000}]})
    path = root / "coders" / ann.headers["X-Coder"] / "themes.json"
    before = path.read_text()
    # Ben's own tool, on his own machine: a fresh registry over the synced folder.
    registry = RecordingRegistry()
    registry.add_folder(root)
    bens_tool = TestClient(create_app(registry), headers={"X-Coder": ben.headers["X-Coder"]})

    bens_tool.get("/api/library/themes", params={"mode": "collaborative"})

    assert path.read_text() == before


def test_a_request_from_an_unknown_coder_writes_nothing(api):
    ann, _, rid, root = api
    trust = text_code(ann, rid, "trust")
    ann.post(f"/api/library/text-codebook/{trust['id']}/move", json={"final": True})
    common = next(c for c in ann.get("/api/library/codes").json()["codes"] if c["coder"] == "common")
    theme = ann.post("/api/library/themes", json={"title": "T", "box": BOX}).json()["theme"]
    ann.post("/api/library/canvas/place", json={"ref": common["ref"], "theme_id": theme["id"], "x": 40, "y": 120})
    ann.post(f"/api/library/themes/{theme['id']}/move", json={"final": True})
    registry = ann.app.state.registry
    registry.books.common_text.remove(common["id"], coder=ann.headers["X-Coder"])  # deleted, card left behind

    TestClient(ann.app, headers={"X-Coder": "zzz"}).get("/api/library/themes")

    assert not (root / "coders" / "zzz").exists()


def test_a_common_card_for_a_code_not_yet_synced_here_is_kept(api):
    ann, _, rid, root = api
    trust = text_code(ann, rid, "trust")
    ann.post(f"/api/library/text-codebook/{trust['id']}/move", json={"final": True})
    common_ref = next(c["ref"] for c in ann.get("/api/library/codes").json()["codes"] if c["coder"] == "common")
    theme = ann.post("/api/library/themes", json={"title": "T", "box": BOX}).json()["theme"]
    ann.post("/api/library/canvas/place", json={"ref": common_ref, "theme_id": theme["id"], "x": 40, "y": 120})
    ann.post(f"/api/library/themes/{theme['id']}/move", json={"final": True})
    # The common codebook copy that names this code has not reached this machine yet.
    ann.app.state.registry.books.common_text._merged.pop(common_ref.split(":", 1)[1])

    [common_theme] = ann.get("/api/library/themes").json()["themes"]
    assert common_theme["refs"] == [common_ref]


def test_a_lassoed_theme_leaves_your_other_themes_alone(api):
    ann, _, rid, _ = api
    trust = text_code(ann, rid, "trust")
    ref = f"text:{trust['id']}"
    own = ann.post("/api/library/themes", json={"title": "Own", "box": BOX}).json()["theme"]
    ann.post("/api/library/canvas/place", json={"ref": ref, "theme_id": own["id"], "x": 40, "y": 120})
    quote_ref = ann.get("/api/library/quotes").json()["quotes"][0]["ref"]

    ann.post("/api/library/themes/from-refs", json={"title": "Lasso", "refs": [quote_ref]})

    themes = {t["title"]: t["refs"] for t in ann.get("/api/library/themes").json()["themes"]}
    assert themes == {"Own": [ref], "Lasso": [ref]}


def test_returning_a_code_puts_your_own_code_back_in_your_themes(api):
    ann, _, rid, _ = api
    trust = text_code(ann, rid, "trust")
    theme = ann.post("/api/library/themes", json={"title": "T", "box": BOX}).json()["theme"]
    ann.post("/api/library/canvas/place", json={"ref": f"text:{trust['id']}", "theme_id": theme["id"], "x": 40, "y": 120})
    ann.post(f"/api/library/text-codebook/{trust['id']}/move", json={"final": True})
    common = next(c for c in ann.get("/api/library/codes").json()["codes"] if c["coder"] == "common")

    ann.post(f"/api/library/text-codebook/{common['id']}/return")

    mine = next(c for c in ann.get("/api/library/codes").json()["codes"] if c["name"] == "trust")
    assert mine["coder"] == ann.headers["X-Coder"]
    assert ann.get("/api/library/themes").json()["themes"][0]["refs"] == [mine["ref"]]
