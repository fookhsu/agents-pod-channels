"""Offline tests for the drain's intake rules (issue #27, ADR 0024).

Nothing here touches the network, the environment or a store: whether one
message becomes an Inbox item is a pure decision, so the tests feed hand-written
message dicts and assert on the JSONL a pass would print.

Run from this directory: python3 -m unittest test_feishu_drain -v
"""

import io
import json
import unittest
from contextlib import redirect_stderr

import feishu_drain as drain

CHAT_ID = "oc_chat"
CHAT_NAME = "测试群"
BASE_AT = 1_700_000_000


def text_message(msg_id, text, at=BASE_AT, sender_type="user", mentions=None):
    message = {
        "message_id": msg_id,
        "msg_type": "text",
        "create_time": str(at * 1000),
        "sender": {
            "id": "ou_human",
            "id_type": "open_id",
            "sender_type": sender_type,
            "tenant_key": "tenant",
        },
        "body": {"content": json.dumps({"text": text}, ensure_ascii=False)},
    }
    if mentions is not None:
        message["mentions"] = mentions
    return message


def system_message(msg_id, at=BASE_AT + 1):
    """The measured shape: a template body and an all-empty sender."""
    return {
        "message_id": msg_id,
        "msg_type": "system",
        "create_time": str(at * 1000),
        "sender": {"id": "", "id_type": "", "sender_type": "", "tenant_key": ""},
        "body": {
            "content": json.dumps(
                {
                    "template": "{from_user} invited {to_chatters} to the chat.",
                    "from_user": ["徐福洋"],
                    "to_chatters": ["邹文", "大肥鱼"],
                },
                ensure_ascii=False,
            )
        },
    }


def run_pass(messages, *, cursor=None, require_mention=False, open_id=None, max_items=40):
    """One pass over a message list, as main() would assemble it."""
    state = drain.new_pass(require_mention=require_mention, open_id=open_id)
    kinds = [drain.absorb(state, msg, CHAT_ID, CHAT_NAME) for msg in messages]
    collected = state["collected"]
    collected.sort(key=lambda item: item["at"])
    emitted = collected[:max_items]
    lines = drain.output_lines(
        emitted,
        drain.advance_cursor(cursor, emitted, collected, state["consumed_at"]),
    )
    return kinds, [json.loads(line) for line in lines]


def listed_chat(chat_id, name="群"):
    """A chat as list_chats returns it — the inject fixture's "listed" side."""
    return {"chat_id": chat_id, "name": name, "chat_id_type": "chat"}


def inject(listed_ids, *, extra="oc_p2p"):
    """inject_extra_chats over a listed set, as the two call sites do."""
    return drain.inject_extra_chats(
        [listed_chat(chat_id) for chat_id in listed_ids], extra
    )


class SystemEventTest(unittest.TestCase):
    def test_system_only_pass_prints_cursor_and_no_item(self):
        kinds, lines = run_pass([system_message("om_sys", at=BASE_AT)])

        self.assertEqual(kinds, ["system"])
        self.assertEqual(lines, [{"cursor": str(BASE_AT)}])

    def test_system_then_human_yields_only_the_human_and_cursor_past_both(self):
        kinds, lines = run_pass(
            [
                system_message("om_sys", at=BASE_AT),
                text_message("om_human", "大家好", at=BASE_AT + 5),
            ]
        )

        self.assertEqual(kinds, ["system", "item"])
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["id"], "om_human")
        self.assertEqual(lines[0]["body"], "大家好")
        self.assertNotIn("system", lines[0]["body"])
        self.assertEqual(lines[1], {"cursor": str(BASE_AT + 5)})

    def test_system_event_is_logged_once_on_stderr(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            drain.note_system_event(system_message("om_sys"), CHAT_ID)

        self.assertEqual(
            stderr.getvalue().strip(),
            "[feishu-drain] skip system event om_sys in %s" % CHAT_ID,
        )


class ExistingFiltersTest(unittest.TestCase):
    def test_require_mention_drops_unmentioned_human(self):
        mentioned = [
            {
                "key": "@bot",
                "id": {"union_id": "on_bot", "user_id": "u_bot", "open_id": "ou_bot"},
                "name": "pod",
            }
        ]
        kinds, lines = run_pass(
            [
                text_message("om_plain", "没人叫我", at=BASE_AT),
                text_message("om_at", "@pod 在吗", at=BASE_AT + 1, mentions=mentioned),
            ],
            require_mention=True,
            open_id="ou_bot",
        )

        self.assertEqual(kinds, ["skip", "item"])
        self.assertEqual([line["id"] for line in lines[:-1]], ["om_at"])
        self.assertEqual(lines[-1], {"cursor": str(BASE_AT + 1)})

    def test_app_sender_is_still_dropped(self):
        kinds, lines = run_pass(
            [text_message("om_bot", "机器人发言", at=BASE_AT, sender_type="app")]
        )

        self.assertEqual(kinds, ["skip"])
        self.assertEqual(lines, [{"cursor": str(BASE_AT)}])

    def test_at_all_mention_does_not_crash_and_still_drops(self):
        """The measured shape from a live tenant: @所有人 carries
        mention.id == "all" (a string), not a dict. It is not the bot, so the
        message is skipped — the bug was the crash before the skip (#126)."""
        at_all = [{"key": "@所有人", "id": "all", "name": "所有人"}]
        kinds, lines = run_pass(
            [text_message("om_all", "@所有人 请查收", at=BASE_AT, mentions=at_all)],
            require_mention=True,
            open_id="ou_bot",
        )

        self.assertEqual(kinds, ["skip"])
        self.assertEqual(lines, [{"cursor": str(BASE_AT)}])

    def test_at_all_before_bot_mention_no_longer_stalls_the_cursor(self):
        """The failure mode that froze a live cursor for three days: an @所有人
        message ahead of a real @bot one made every pass crash, so the cursor
        never crossed it. A pass must read past the "all" mention to the item."""
        at_all = [{"key": "@所有人", "id": "all", "name": "所有人"}]
        mentioned = [
            {"key": "@bot", "id": {"union_id": "on", "open_id": "ou_bot"}, "name": "pod"}
        ]
        kinds, lines = run_pass(
            [
                text_message("om_all2", "@所有人 开会", at=BASE_AT, mentions=at_all),
                text_message("om_at2", "@pod 收到", at=BASE_AT + 1, mentions=mentioned),
            ],
            require_mention=True,
            open_id="ou_bot",
        )

        self.assertEqual(kinds, ["skip", "item"])
        self.assertEqual([line["id"] for line in lines[:-1]], ["om_at2"])
        self.assertEqual(lines[-1], {"cursor": str(BASE_AT + 1)})


class ExtraChatsTest(unittest.TestCase):
    """FEISHU_EXTRA_CHATS: p2p chats the enumeration cannot see (ADR 0024).

    Pure on the same discipline as the rest of this file: the raw variable
    value is a parameter, os.environ is never touched.
    """

    def test_extra_id_not_listed_is_injected_with_p2p_mark(self):
        chats = inject(["oc_group"])
        self.assertEqual(
            chats,
            [
                {"chat_id": "oc_group", "name": "群", "chat_id_type": "chat"},
                {"chat_id": "oc_p2p", "name": "oc_p2p", "p2p": True},
            ],
        )

    def test_already_listed_id_is_not_duplicated(self):
        chats = inject(["oc_group"], extra="oc_group")
        self.assertEqual(len(chats), 1)

    def test_blank_and_comma_edge_values_are_ignored(self):
        for raw in (None, "", "  ", ",,"):
            chats = inject([], extra=raw)
            self.assertEqual(chats, [])
        chats = inject([], extra=" oc_a ,, ")
        self.assertEqual([c["chat_id"] for c in chats], ["oc_a"])
        chats = inject([], extra=" oc_a ,, oc_b ")
        self.assertEqual([c["chat_id"] for c in chats], ["oc_a", "oc_b"])


class CursorTest(unittest.TestCase):
    def test_nothing_read_prints_nothing(self):
        _, lines = run_pass([], cursor=BASE_AT)

        self.assertEqual(lines, [])

    def test_cursor_does_not_jump_a_queued_item(self):
        messages = [
            text_message("om_1", "一", at=BASE_AT),
            text_message("om_2", "二", at=BASE_AT + 1),
            text_message("om_3", "三", at=BASE_AT + 2),
        ]
        _, lines = run_pass(messages, max_items=2)

        self.assertEqual([line["id"] for line in lines[:-1]], ["om_1", "om_2"])
        self.assertEqual(lines[-1], {"cursor": str(BASE_AT + 1)})


if __name__ == "__main__":
    unittest.main()
