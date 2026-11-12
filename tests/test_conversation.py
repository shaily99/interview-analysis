"""Tests for parsing and rendering a recording's conversation.jsonl."""

import json

from subtitle_search.conversation import CONVERSATION_FILENAME, load_conversation


def msg(role, text, turn=0, version=0, cid="c1", **extra):
    return {"record_type": "message", "conversation_id": cid, "turn": turn,
            "version": version, "role": role, "text": text, **extra}


def write_conversation(folder, records, tail=""):
    (folder / CONVERSATION_FILENAME).write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records) + tail
    )


def test_absent_file_is_none(tmp_path):
    assert load_conversation(tmp_path) is None


def test_plain_exchange(tmp_path):
    write_conversation(tmp_path, [msg("user", "hi"), msg("assistant", "**bold**")])
    conv = load_conversation(tmp_path)
    assert conv.order == ["c1:0:0:user", "c1:0:0:assistant"]
    assert 'data-message-id="c1:0:0:user"' in conv.html
    assert "<strong>bold</strong>" in conv.html
    meta = conv.messages["c1:0:0:assistant"]
    assert meta == {"role": "assistant", "turn": 0, "version": 0, "conversation_id": "c1",
                    "conversation_index": 0, "conversation_name": None, "abandoned": False}


def test_raw_html_escaped_and_links_inert(tmp_path):
    write_conversation(tmp_path, [
        msg("user", "hi"), msg("assistant", "<script>x</script> [a](http://e.com)")])
    html = load_conversation(tmp_path).html
    assert "&lt;script&gt;" in html
    assert 'data-href="http://e.com"' in html
    assert 'title="http://e.com"' in html
    assert "href=" not in html.replace("data-href=", "")


def test_retry_hangs_under_reply(tmp_path):
    write_conversation(tmp_path, [
        msg("user", "hi"), msg("assistant", "one"),
        {"record_type": "reply_retried", "conversation_id": "c1", "retried_turn": 0},
        msg("user", "hi", version=1), msg("assistant", "two", version=1),
    ])
    conv = load_conversation(tmp_path)
    assert conv.order == ["c1:0:0:user", "c1:0:0:assistant", "c1:0:1:assistant"]
    assert conv.html.count("User regenerated this reply.") == 1
    assert "c1:0:1:user" not in conv.messages


def test_edit_abandons_and_marks(tmp_path):
    write_conversation(tmp_path, [
        msg("user", "hi"), msg("assistant", "one"),
        {"record_type": "message_edited", "conversation_id": "c1", "edited_turn": 0},
        msg("user", "hello", version=1), msg("assistant", "two", version=1),
    ])
    conv = load_conversation(tmp_path)
    assert conv.messages["c1:0:0:user"]["abandoned"] is True
    assert conv.messages["c1:0:0:assistant"]["abandoned"] is True
    assert conv.messages["c1:0:1:user"]["abandoned"] is False
    assert "User edited their message" in conv.html
    gone = conv.html.split('class="tf-gone">')[1].split("User edited their message")[0]
    assert "c1:0:0:user" in gone and "c1:0:1:user" not in gone


def test_cleared_conversations_named(tmp_path):
    write_conversation(tmp_path, [
        msg("user", "hi"), msg("assistant", "a"),
        msg("user", "again", cid="c2"), msg("assistant", "b", cid="c2"),
    ])
    conv = load_conversation(tmp_path)
    assert conv.messages["c1:0:0:user"]["conversation_name"] == "First"
    assert conv.messages["c2:0:0:user"]["conversation_name"] == "Second"
    assert conv.messages["c2:0:0:user"]["conversation_index"] == 1
    assert "User cleared the conversation and started again." in conv.html


def test_error_reply_omitted(tmp_path):
    write_conversation(tmp_path, [msg("user", "hi"), msg("assistant", "boom", error="x")])
    assert load_conversation(tmp_path).order == ["c1:0:0:user"]


def test_interrupted_label_outside_anchor(tmp_path):
    write_conversation(tmp_path, [msg("user", "hi"), msg("assistant", "part", interrupted=True)])
    html = load_conversation(tmp_path).html
    anchor = html.index('data-message-id="c1:0:0:assistant"')
    label = html.index("This generation was interrupted.")
    assert label > html.index("</div>", anchor)


def test_quoted_text_inside_user_anchor(tmp_path):
    write_conversation(tmp_path, [
        msg("user", '"old words" typed', quoted_text="old words"), msg("assistant", "a")])
    html = load_conversation(tmp_path).html
    start = html.index('data-message-id="c1:0:0:user"')
    end = html.index("tf-avatar--you", start)
    inside = html[start:end]
    assert "tf-quote-line" in inside and "tf-bubble" in inside and "typed" in inside


def test_unreadable_line_skipped(tmp_path):
    write_conversation(tmp_path, [msg("user", "hi"), msg("assistant", "a")],
                       tail='{"record_type": "mess')
    assert load_conversation(tmp_path).order == ["c1:0:0:user", "c1:0:0:assistant"]


def test_line_separator_inside_text_does_not_split_record(tmp_path):
    write_conversation(tmp_path, [msg("user", "hi"), msg("assistant", "before\u2028after\x0cend")])
    conv = load_conversation(tmp_path)
    assert conv.order == ["c1:0:0:user", "c1:0:0:assistant"]
    assert "before\u2028after" in conv.html
