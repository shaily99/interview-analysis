"""Edits, deletions and removals on a common code appear in its history; a coder's own code keeps none."""

import pytest
from fastapi.testclient import TestClient

from subtitle_search.app import create_app
from subtitle_search.session import RecordingRegistry

from . import fixtures

QUOTE = {"text": "Cool.", "start_cue_id": "c0", "start_char_offset": 0, "end_cue_id": "c0", "end_char_offset": 5}


@pytest.fixture
def client(tmp_path):
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX, encoding="utf-8")
    registry = RecordingRegistry()
    registry.add_folder(tmp_path)
    coder = registry.coders.create("Test Coder", "TC")
    return TestClient(create_app(registry), headers={"X-Coder": coder["id"]}), registry.default.id


def common_code(http, rid, kind, name):
    """A code of the given kind, used once, then moved to common; returns the common code's id."""
    code = http.post(f"/api/library/{kind}-codebook", json={"name": name}).json()["code"]
    if kind == "text":
        http.post(f"/api/recordings/{rid}/highlights", json={**QUOTE, "codes": [code["id"]]})
    else:
        http.post(f"/api/recordings/{rid}/video-codes", json={"code_id": code["id"], "start": 1, "end": 2})
    http.post(f"/api/library/{kind}-codebook/{code['id']}/move", json={"final": True})
    return next(c["id"] for c in http.get(f"/api/library/{kind}-codebook").json()["codes"] if c["coder"] == "common")


def history(http, kind, code_id):
    return http.get(f"/api/library/{kind}-codebook/{code_id}/history").json()["entries"]


@pytest.mark.parametrize("kind", ["text", "video"])
def test_renaming_a_common_code_records_the_old_name_and_what_changed(client, kind):
    http, rid = client
    code_id = common_code(http, rid, kind, "trust")

    http.patch(f"/api/library/{kind}-codebook/{code_id}", json={"name": "reliance", "description": "leans on it"})

    edited = history(http, kind, code_id)[0]
    assert (edited["action"], edited["name"], edited["from"]) == ("edited", "reliance", "trust")
    assert sorted(edited["changes"]) == ["description", "name"]


@pytest.mark.parametrize("kind", ["text", "video"])
def test_removing_a_common_codes_uses_then_deleting_it_is_recorded(client, kind):
    http, rid = client
    code_id = common_code(http, rid, kind, "trust")
    refs = [a["ref"] for a in http.get(f"/api/library/{kind}-codebook/{code_id}/applications").json()["applications"]]

    http.post(f"/api/library/{kind}-codebook/{code_id}/applications/remove", json={"refs": refs})
    assert http.delete(f"/api/library/{kind}-codebook/{code_id}").status_code == 200

    entries = history(http, kind, code_id)
    assert [e["action"] for e in entries] == ["deleted", "removed", "moved"]
    assert entries[1]["removed"] == 1
    assert entries[0]["name"] == "trust"


@pytest.mark.parametrize("kind", ["text", "video"])
def test_edits_to_a_coders_own_code_record_nothing(client, kind):
    http, rid = client
    code = http.post(f"/api/library/{kind}-codebook", json={"name": "trust"}).json()["code"]

    http.patch(f"/api/library/{kind}-codebook/{code['id']}", json={"name": "reliance"})
    http.delete(f"/api/library/{kind}-codebook/{code['id']}")

    assert history(http, kind, code["id"]) == []
