"""A participant's chat log (`conversation.jsonl`), parsed and rendered as HTML for the Conversation pane.

Ported from `study_interface/turn_feedback.py` in the study-interface repository (`build_transcript`
and its HTML functions), so the two copies can be compared. It reads a record list instead of a
session, labels markers by "User" instead of "you", drops the marked-reply chip and the transcript
header, and gives every message one anchor element carrying `data-message-id`.
"""

import html
import json
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt

CONVERSATION_FILENAME = "conversation.jsonl"

# The two participant actions that add to a conversation without sending a new message. Both are
# recorded before the resubmission they cause, so the record that follows one is the resubmitted message.
RETRY = "retry"
EDIT = "edit"

# What the participant did between two parts of the transcript.
REGENERATED_LABEL = "User regenerated this reply."
EDITED_LABEL = "User edited their message"
CLEARED_LABEL = "User cleared the conversation and started again."

# Closes a reply that stopped part way.
INTERRUPTED_LABEL = "This generation was interrupted."

# Names for the conversations of a file that holds more than one.
CONVERSATION_NAMES = ("First", "Second", "Third", "Fourth", "Fifth")

# Raw HTML is disabled, which escapes any markup a reply contains rather than putting it in the page.
_MARKDOWN = MarkdownIt("commonmark", {"html": False})


def _inert_link_open(renderer, tokens, idx, options, env):
    """Render a link's opening tag with its address under `data-href` and `title` in place of `href`.

    The link stays inert, but is styled as one and shows the address on hover, as the participant saw it.
    """
    token = tokens[idx]
    href = token.attrs.pop("href", None)
    if href is not None:
        token.attrSet("data-href", href)
        token.attrSet("title", href)
    return renderer.renderToken(tokens, idx, options, env)


_MARKDOWN.add_render_rule("link_open", _inert_link_open)


@dataclass
class Conversation:
    """A rendered chat log.

    Attributes:
        html: the whole transcript, one element per conversation.
        order: message ids in the order they appear in `html`.
        messages: per message id, `role`, `turn`, `version`, `conversation_id`, `conversation_index`,
            `conversation_name` (None unless the file holds more than one conversation) and
            `abandoned` (True inside an exchange an edit or retry discarded).
    """

    html: str
    order: list[str]
    messages: dict[str, dict]


def message_id(record):
    """Give a message's stable id, `<conversation_id>:<turn>:<version>:<role>`, from its own record."""
    return f"{record.get('conversation_id')}:{record.get('turn')}:{record.get('version')}:{record.get('role')}"


def _key(reply):
    """Give the identity of one reply, as `(conversation_id, turn, version)`."""
    return (reply.get("conversation_id"), reply.get("turn"), reply.get("version"))


def _shown(reply):
    """Whether a reply is one the participant read: present, without an `error`, and with text."""
    return (
        reply is not None
        and reply.get("error") is None
        and (reply.get("text") or "").strip() != ""
    )


def _open_exchange(conversation, turn):
    """Give the exchange a retry at `turn` adds its reply to: the last one recorded at that position, or None."""
    for exchange in reversed(conversation["exchanges"]):
        if exchange["turn"] == turn:
            return exchange
    return None


def _abandon(conversation, turn, inclusive):
    """Mark the exchanges an edit or a retry at `turn` discarded, in place.

    Args:
        conversation: one conversation from `build_transcript`.
        turn: the position the edit or retry names.
        inclusive: True for an edit, which discards the exchange at `turn` as well.
    """
    for exchange in conversation["exchanges"]:
        if exchange["turn"] > turn or (inclusive and exchange["turn"] == turn):
            exchange["abandoned"] = True


def build_transcript(records):
    """Group a conversation log into conversations, each a list of exchanges.

    Two passes: the first keys every reply by `(conversation_id, turn, version)`, because an abandoned
    reply's record can land after the record for the message that replaced it; the second walks the
    records in order.

    Args:
        records: the parsed records of `conversation.jsonl`, in file order.

    Returns:
        list: one dict per conversation, oldest first, each holding `conversation_id`, `cleared` and
        `exchanges`. Each exchange holds `turn`, `user` (the user `message` record), `prompt`,
        `quoted_text`, `replies` (oldest first, only those `_shown` admits), `after_edit` and
        `abandoned`.
    """
    replies = {
        _key(record): record
        for record in records
        if record.get("record_type") == "message" and record.get("role") == "assistant"
    }

    conversations = []
    by_id = {}
    # Set by an edit or a retry, and consumed by the resubmitted message that follows it.
    pending = None

    for record in records:
        record_type = record.get("record_type")

        if record_type == "reply_retried":
            pending = (RETRY, record.get("conversation_id"), record.get("retried_turn"))
            conversation = by_id.get(record.get("conversation_id"))
            if conversation is not None:
                _abandon(conversation, record.get("retried_turn"), inclusive=False)

        elif record_type == "message_edited":
            pending = (EDIT, record.get("conversation_id"), record.get("edited_turn"))
            conversation = by_id.get(record.get("conversation_id"))
            if conversation is not None:
                _abandon(conversation, record.get("edited_turn"), inclusive=True)

        elif record_type == "message" and record.get("role") == "user":
            conversation_id = record.get("conversation_id")
            turn = record.get("turn")

            conversation = by_id.get(conversation_id)
            if conversation is None:
                conversation = {
                    "conversation_id": conversation_id,
                    "cleared": False,
                    "exchanges": [],
                }
                by_id[conversation_id] = conversation
                conversations.append(conversation)

            action, marked_conversation, marked_turn = pending or (None, None, None)
            resubmitted = (marked_conversation, marked_turn) == (conversation_id, turn)
            pending = None

            reply = replies.get((conversation_id, turn, record.get("version")))
            open_exchange = _open_exchange(conversation, turn) if resubmitted else None

            # A retry resends the message already there, so its reply joins the exchange rather than opening one.
            if action == RETRY and open_exchange is not None:
                if _shown(reply):
                    open_exchange["replies"].append(reply)
                continue

            conversation["exchanges"].append(
                {
                    "turn": turn,
                    "user": record,
                    "prompt": record.get("text") or "",
                    "quoted_text": record.get("quoted_text"),
                    "replies": [reply] if _shown(reply) else [],
                    "after_edit": action == EDIT and resubmitted,
                    "abandoned": False,
                }
            )

    for conversation in conversations[:-1]:
        conversation["cleared"] = True

    return conversations


def _marker(kind, label):
    """Draw one break in the transcript, named by what the participant did to cause it."""
    return f'<p class="tf-mark tf-mark--{kind}"><span>{html.escape(label)}</span></p>'


def _participant_html(text, quoted_text, anchor):
    """Draw one participant message, with a muted quote line above the bubble if it quotes a reply.

    The text is escaped rather than rendered as Markdown, with line breaks kept. The bubble stack, which
    holds the quote line and the bubble, carries the message anchor.

    Args:
        text: the message as sent, `"<quoted_text>" <typed text>` when `quoted_text` is set.
        quoted_text: the exact substring highlighted, or None.
        anchor: the `data-message-id` attribute to place on the bubble stack.
    """
    if quoted_text:
        quoted = f'"{quoted_text}"'
        typed = text[len(quoted) :].lstrip() if text.startswith(quoted) else text
        bubbles = f'<div class="tf-quote-line">{html.escape(quoted)}</div>'
        # A quote-only send has nothing left for a bubble to show.
        if typed:
            bubbles += f'<div class="tf-bubble">{html.escape(typed).replace(chr(10), "<br>")}</div>'
    else:
        bubbles = f'<div class="tf-bubble">{html.escape(text).replace(chr(10), "<br>")}</div>'

    return (
        '<div class="tf-row tf-row--you">'
        f'<div class="tf-bubble-stack" {anchor}>{bubbles}</div>'
        '<span class="tf-avatar tf-avatar--you"></span>'
        "</div>"
    )


def _reply_html(reply, anchor):
    """Draw one reply. The Markdown body carries the message anchor; the interrupted label follows it."""
    body = _MARKDOWN.render(reply.get("text") or "")
    label = ""
    if reply.get("interrupted"):
        label = f'<p class="tf-cut">{html.escape(INTERRUPTED_LABEL)}</p>'

    return (
        '<div class="tf-row tf-row--ai">'
        '<span class="tf-avatar tf-avatar--ai"></span>'
        f'<div class="tf-reply"><div class="tf-body" {anchor}>{body}</div>{label}</div>'
        "</div>"
    )


def _exchange_html(exchange, register):
    """Draw one exchange: the participant's message, then every reply it produced.

    Args:
        exchange: one exchange from `build_transcript`.
        register: called with each message record and whether it is abandoned as it is drawn, returning its anchor attribute.
    """
    parts = [_participant_html(exchange["prompt"], exchange.get("quoted_text"), register(exchange["user"], exchange["abandoned"]))]

    for position, reply in enumerate(exchange["replies"]):
        if position:
            parts.append(_marker("retry", REGENERATED_LABEL))
        parts.append(_reply_html(reply, register(reply, exchange["abandoned"])))

    return "".join(parts)


def _conversation_html(conversation, position, named, register):
    """Draw one conversation, wrapping consecutive abandoned exchanges together in one `tf-gone` block.

    Args:
        conversation: one conversation from `build_transcript`.
        position: its place in the file, counted from 0.
        named: True to label it, which a file of one conversation has no reason to do.
        register: as in `_exchange_html`.
    """
    parts = []
    if named:
        name = _conversation_name(position)
        parts.append(f'<p class="tf-conv-label">{html.escape(name)} conversation</p>')

    abandoned = []

    def close_abandoned():
        if abandoned:
            parts.append(f'<div class="tf-gone">{"".join(abandoned)}</div>')
            abandoned.clear()

    for exchange in conversation["exchanges"]:
        # The edit is drawn between the exchange it replaced and the one it produced.
        if exchange["after_edit"]:
            close_abandoned()
            parts.append(_marker("edit", EDITED_LABEL))

        rendered = _exchange_html(exchange, register)
        if exchange["abandoned"]:
            abandoned.append(rendered)
        else:
            close_abandoned()
            parts.append(rendered)

    close_abandoned()

    classes = "tf-conv tf-conv--named" if named else "tf-conv"
    return f'<div class="{classes}">{"".join(parts)}</div>'


def _conversation_name(position):
    """Give a conversation's name by its position from 0."""
    if position < len(CONVERSATION_NAMES):
        return CONVERSATION_NAMES[position]
    return f"Conversation {position + 1}"


def _read_records(path):
    """Read the JSON objects of a JSONL file, skipping lines that are not one (such as a half-synced last line)."""
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").split("\n"):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def load_conversation(folder):
    """Read and render a recording folder's `conversation.jsonl`.

    Args:
        folder: the recording folder, as a `Path`.

    Returns:
        Conversation: the rendered log, or None when the folder holds no `conversation.jsonl`.
    """
    path = Path(folder) / CONVERSATION_FILENAME
    if not path.is_file():
        return None

    conversations = build_transcript(_read_records(path))
    named = len(conversations) > 1
    order = []
    messages = {}

    parts = []
    for position, conversation in enumerate(conversations):
        if position:
            parts.append(_marker("clear", CLEARED_LABEL))

        # Draws happen in page order, so registering as each message is drawn gives `order`.
        def register(record, abandoned, position=position):
            mid = message_id(record)
            order.append(mid)
            messages[mid] = {
                "role": record.get("role"),
                "turn": record.get("turn"),
                "version": record.get("version"),
                "conversation_id": record.get("conversation_id"),
                "conversation_index": position,
                "conversation_name": _conversation_name(position) if named else None,
                "abandoned": abandoned,
            }
            return f'data-message-id="{html.escape(mid, quote=True)}"'

        parts.append(_conversation_html(conversation, position, named, register))

    return Conversation(html="".join(parts), order=order, messages=messages)
