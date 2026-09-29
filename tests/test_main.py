from subtitle_search.__main__ import _dump_parse as dump_parse
from subtitle_search.session import RecordingRegistry

from . import fixtures


def test_dump_parse_lists_quotes_per_coder(tmp_path, capsys):
    (tmp_path / "meeting.vtt").write_text(fixtures.COLON_PREFIX)
    registry = RecordingRegistry()
    registry.add_folder(tmp_path)
    ann = registry.coders.create("Ann Lee", "AL")
    registry.default.store.create(
        ann["id"], {"text": "Cool.", "start_cue_id": "c0", "start_char_offset": 0, "end_cue_id": "c0", "end_char_offset": 5}
    )

    dump_parse(registry)

    assert "quotes     : 1 (AL 1)" in capsys.readouterr().out
