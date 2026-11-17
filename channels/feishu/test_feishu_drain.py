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
