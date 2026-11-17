#!/usr/bin/env python3
"""Feishu pull drain — an agents-pod Source command (ADR 0024).

One invocation = one reconcile pass. Reads every group chat the bot is in
(plus the thread containers those chats reveal) from the committed cursor,
prints one JSONL item per human message, then the new cursor. Chat `system`
events (invites, renames, pins) are logged to stderr instead: an event is
something the conversation did, not something a human said (ADR 0024).

Environment (all optional, read from `~/.agents/pods/adapters/.env` too):
  FEISHU_APP_ID / FEISHU_APP_SECRET   the bot app credentials
  FEISHU_CHATS                        comma-separated chat_id allowlist (empty: all)
  FEISHU_REQUIRE_MENTION              1 = only messages that @-mention the bot
  FEISHU_LOOKBACK_SECONDS             read this far back when there is no cursor (3600)
  FEISHU_OVERLAP_SECONDS              re-read overlap behind the cursor (60)
  FEISHU_MAX_ITEMS                    at most this many items per drain (40)
  --check                             print a self-check instead of draining (ADR 0027):
                                      exit 0 healthy, 1 not; `agents-pod doctor --probe`
                                      runs it and quotes these lines

The cursor never advances past an item this drain did not print, so a burst
bigger than FEISHU_MAX_ITEMS simply arrives over several passes. p2p chats are
not enumerable (ADR 0024), so only group conversations are read.
"""

import json
import os
import re
import sys
import time

import feishu_client as fc

PAGE_SIZE = 50
THREAD_PAGE_CEILING = 6
THREAD_KEEP_SECONDS = 7 * 24 * 3600
SYSTEM_MSG_TYPE = "system"


def env_int(name, default):
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


def env_flag(name):
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def warn(message):
    sys.stderr.write("[feishu-drain] %s\n" % message)


def list_chats(token):
    """Every chat the bot can see; the chat list itself excludes p2p (ADR 0024)."""
    chats, page_token = [], ""
    while True:
        data = fc.api("GET", "/im/v1/chats", token=token,
                      params={"page_size": 100, "page_token": page_token})
        chats.extend(data.get("items") or [])
        page_token = data.get("page_token") or ""
        if not data.get("has_more") or not page_token:
            return chats


def iter_messages(token, container_type, container_id, since=None):
    """Pages one container in ascending create-time order."""
    page_token, pages = "", 0
    while True:
        params = {
            "container_id_type": container_type,
            "container_id": container_id,
            "sort_type": "ByCreateTimeAsc",
            "page_size": PAGE_SIZE,
            "page_token": page_token,
            "start_time": since if since else None,
        }
        data = fc.api("GET", "/im/v1/messages", token=token, params=params)
        for msg in data.get("items") or []:
            yield msg
        page_token = data.get("page_token") or ""
        pages += 1
        if not data.get("has_more") or not page_token:
            return
        if container_type == "thread" and pages >= THREAD_PAGE_CEILING:
            warn("thread %s: stopped after %d pages (ceiling)" % (container_id, pages))
            return


def created_at(msg):
    try:
        return int(msg.get("create_time") or 0) // 1000
    except (TypeError, ValueError):
        return 0


def is_bot(msg):
    return ((msg.get("sender") or {}).get("sender_type") or "") == "app"


def mentions_bot(msg, open_id):
    if not open_id:
        return True
    for mention in msg.get("mentions") or []:
        if (mention.get("id") or {}).get("open_id") == open_id:
            return True
    return False


def is_system(msg):
    """A conversation event — invite, rename, pin — rather than a message."""
    return (msg.get("msg_type") or "") == SYSTEM_MSG_TYPE


def is_item(msg, *, require_mention, open_id):
    """Whether one message is Inbox material (ADR 0024).

    The `system` exclusion is about what a conversation contains, not about
    identity: an event has no author to answer, so it cannot become an item.
    `sender_type == "app"` stays a separate filter — a bot message is still a
    message, just not one the pod needs to answer.
    """
    if not msg.get("message_id"):
        return False
    if is_bot(msg):
        return False
    if is_system(msg):
        return False
    if require_mention and not mentions_bot(msg, open_id):
        return False
    return True


def item_from(msg, chat_id, chat_name):
    """The Inbox record for one message, or None when it flattens to nothing.

    Pure, so the item's shape is testable without a tenant (ADR 0024: `id`,
    `body` and the optional routing fields).
    """
    body = fc.flatten_message(msg)
    if not body:
        return None
    msg_id = msg.get("message_id")
    return {
        "id": msg_id,
        "title": "%s: %s" % (chat_name, body.splitlines()[0][:60]),
        "body": body,
        "chat_id": chat_id,
        "root_id": msg.get("root_id") or msg.get("parent_id") or msg_id,
        "thread_id": msg.get("thread_id") or "",
        "at": created_at(msg),
    }


def new_pass(*, require_mention, open_id):
    """The per-pass intake state: what this pass saw, kept, and consumed."""
    return {
        "seen": set(),
        "collected": [],
        "consumed_at": 0,
        "require_mention": require_mention,
        "open_id": open_id,
    }


def absorb(pass_state, msg, chat_id, chat_name):
    """Fold one message into a pass; returns `item`, `system`, `skip` or `seen`.

    Pure — no network, no environment, no store. A `system` event is consumed
    like any other message (so the cursor crosses it) but yields no item: it
    belongs in the operator's log, not the Inbox.
    """
    msg_id = msg.get("message_id")
    if not msg_id or msg_id in pass_state["seen"]:
        return "seen"
    if is_system(msg):
        pass_state["seen"].add(msg_id)
        pass_state["consumed_at"] = max(pass_state["consumed_at"], created_at(msg))
        return "system"
    if not is_item(msg, require_mention=pass_state["require_mention"],
                   open_id=pass_state["open_id"]):
        pass_state["consumed_at"] = max(pass_state["consumed_at"], created_at(msg))
        return "skip"
    item = item_from(msg, chat_id, chat_name)
    if item is None:
        pass_state["consumed_at"] = max(pass_state["consumed_at"], created_at(msg))
        return "skip"
    pass_state["seen"].add(msg_id)
    pass_state["collected"].append(item)
    pass_state["consumed_at"] = max(pass_state["consumed_at"], item["at"])
    return "item"


def advance_cursor(cursor, emitted, collected, consumed_at):
    """Where the next pass starts (ADR 0024).

    The cursor never jumps a message this pass left for later, so while items
    remain queued it stops at the last one printed. With nothing queued it also
    crosses skipped messages, because they are consumed rather than deferred:
    a pass of nothing but system events still advances, and zero items with a
    cursor stays distinguishable from a drain pointed at nothing (ADR 0027).
    `None` means "no cursor line": the pass read and consumed nothing at all.
    """
    if not emitted and not consumed_at:
        return None  # nothing read and nothing skipped: the "pointed at nothing" shape
    next_cursor = max(cursor or 0, emitted[-1]["at"] if emitted else 0)
    if len(collected) <= len(emitted):
        next_cursor = max(next_cursor, consumed_at)
    return next_cursor


def output_lines(items, cursor):
    """The drain's stdout: one item per line, then the cursor line (ADR 0024).

    `at` is internal ordering only. A cursor-only output is the whole answer of
    a pass that consumed events but had nothing to deliver — deliberately not
    the same shape as empty stdout.
    """
    lines = []
    for item in items:
        record = {key: value for key, value in item.items() if key != "at"}
        lines.append(json.dumps(record, ensure_ascii=False))
    if cursor:
        lines.append(json.dumps({"cursor": str(cursor)}))
    return lines


def note_system_event(msg, chat_id):
    """A skipped event is the operator's line, never an Inbox item."""
    warn("skip system event %s in %s"
         % (msg.get("message_id") or "?", chat_id or "?"))


def main():
    if "--check" in sys.argv[1:]:
        return self_check()
    fc.load_env()
    try:
        token = fc.tenant_token()
    except fc.FeishuError as err:
        warn("no tenant token: %s" % err)
        return 1

    cursor_raw = (os.environ.get("AGENTS_POD_CURSOR") or "").strip()
    now = int(time.time())
    overlap = env_int("FEISHU_OVERLAP_SECONDS", 60)
    lookback = env_int("FEISHU_LOOKBACK_SECONDS", 3600)
    max_items = max(1, env_int("FEISHU_MAX_ITEMS", 40))
    allowlist = {c.strip() for c in (os.environ.get("FEISHU_CHATS") or "").split(",") if c.strip()}
    require_mention = env_flag("FEISHU_REQUIRE_MENTION")

    cursor = int(cursor_raw) if cursor_raw.isdigit() else None
    floor = (cursor if cursor is not None else now - lookback) - overlap
    since = max(0, floor)

    state = fc.load_state()
    known_chats = set(state["known_chats"])
    threads = state["threads"]

    try:
        open_id = fc.bot_open_id(token) if require_mention else None
        chats = list_chats(token)
    except fc.FeishuError as err:
        warn("chat enumeration failed: %s" % err)
        return 1

    chats = [c for c in chats if not allowlist or c.get("chat_id") in allowlist]
    chat_names = {c.get("chat_id"): (c.get("name") or c.get("chat_id")) for c in chats}

    pass_state = new_pass(require_mention=require_mention, open_id=open_id)
    collected = pass_state["collected"]

    def handle(chat_id, msg):
        kind = absorb(pass_state, msg, chat_id,
                      chat_names.get(chat_id, chat_id or "chat"))
        if kind == "system":
            note_system_event(msg, chat_id)
            return
        if kind != "item":
            return
        item = collected[-1]
        thread_id = item["thread_id"]
        # A thread container is the only place its replies appear (ADR 0024).
        if thread_id and thread_id not in threads:
            threads[thread_id] = {"chat_id": chat_id, "seen_at": item["at"] or now}

    for chat in chats:
        chat_id = chat.get("chat_id")
        if not chat_id:
            continue
        # A chat the bot just joined has no cursor of its own; read a lookback.
        chat_since = since if chat_id in known_chats else max(0, now - lookback)
        try:
            for msg in iter_messages(token, "chat", chat_id, chat_since):
                handle(chat_id, msg)
        except fc.FeishuError as err:
            warn("chat %s: %s" % (chat_id, err))
            return 1
        known_chats.add(chat_id)
        chat_names.setdefault(chat_id, chat.get("name") or chat_id)

    for thread_id in list(threads.keys()):
        info = threads[thread_id] or {}
        chat_id = info.get("chat_id") or ""
        if allowlist and chat_id not in allowlist:
            continue
        try:
            for msg in iter_messages(token, "thread", thread_id):
                if created_at(msg) < since:
                    continue
                handle(chat_id or msg.get("chat_id") or "", msg)
        except fc.FeishuError as err:
            warn("thread %s: %s" % (thread_id, err))
            continue
        if now - (info.get("seen_at") or now) > THREAD_KEEP_SECONDS:
            del threads[thread_id]

    state["known_chats"] = sorted(known_chats)
    state["threads"] = threads
    try:
        fc.save_state(state)
    except OSError as err:
        warn("state write failed: %s" % err)

    collected.sort(key=lambda item: item["at"])
    emitted = collected[:max_items]
    next_cursor = advance_cursor(cursor, emitted, collected, pass_state["consumed_at"])

    for line in output_lines(emitted, next_cursor):
        sys.stdout.write(line + "\n")

    if len(collected) > len(emitted):
        warn("%d more messages wait for the next pass" % (len(collected) - len(emitted)))
    sys.stdout.flush()
    return 0


def scope_hint(err):
    """A missing scope names its console link inside the error - or we build one."""
    hint = ["grant it in the app console, then publish a new version (权限管理 → 版本管理与发布)"]
    text = " ".join([err.msg or "", getattr(err, "ext", "") or ""])
    url = None
    for word in re.split(r"[：\s]+", text):
        if word.startswith("https://open.feishu.cn/"):
            url = word
            break
    scopes = re.findall(r"need scope: ([a-z:._]+)", text)
    app_id = (os.environ.get("FEISHU_APP_ID") or "").strip()
    if url is None and scopes and app_id:
        url = ("https://open.feishu.cn/app/%s/auth?q=%s&op_from=openapi&token_type=tenant"
               % (app_id, ",".join(scopes)))
    if url:
        hint.insert(0, url)
    return hint


def self_check():
    """`--check`: the operator command's own report (ADR 0027). Exit 0 healthy, 1 not."""
    fc.load_env()
    failures = 0
    try:
        token = fc.tenant_token()
    except fc.FeishuError as err:
        print("no tenant token: %s" % err)
        print("  credentials live in .env beside this file: cp .env.example .env, then fill")
        print("  FEISHU_APP_ID / FEISHU_APP_SECRET from https://open.feishu.cn/app")
        return 1
    print("token ok (tenant_access_token, cached in .token.json for its 2 h)")

    try:
        bot = fc.api("GET", "/bot/v3/info", token=token).get("bot") or {}
        print("bot %s (%s)" % (bot.get("app_name") or "?", bot.get("open_id") or "?"))
    except fc.FeishuError as err:
        print("bot info failed: %s" % err)
        failures += 1

    allowlist = {c.strip() for c in (os.environ.get("FEISHU_CHATS") or "").split(",") if c.strip()}
    try:
        visible = list_chats(token)
    except fc.FeishuError as err:
        print("chat list failed: %s" % err)
        for line in scope_hint(err):
            print("  " + line)
        return 1
    chats = [c for c in visible if not allowlist or c.get("chat_id") in allowlist]
    if not chats:
        print("no readable chat: this app is in no group chat%s"
              % (" (or FEISHU_CHATS excludes every one)" if allowlist else ""))
        print("  add it: 群设置 → 群机器人 → 添加机器人 %s" % (bot.get("app_name") or ""))
        print("  a p2p chat cannot be enumerated by a pull drain (ADR 0024)")
        return 1
    print("%d chat(s) visible%s" % (len(chats), ", %d allowed by FEISHU_CHATS" % len(allowlist)
                                    if allowlist else ""))

    chat = chats[0]
    name = chat.get("name") or chat.get("chat_id")
    try:
        fc.api("GET", "/im/v1/messages", token=token, params={
            "container_id_type": "chat", "container_id": chat.get("chat_id"),
            "sort_type": "ByCreateTimeAsc", "page_size": 1})
        print("read ok in %s" % name)
    except fc.FeishuError as err:
        print("read failed in %s: %s" % (name, err))
        for line in scope_hint(err):
            print("  " + line)
        failures += 1
    state = fc.load_state()
    print("state: %d known chat(s), %d known thread(s)"
          % (len(state["known_chats"]), len(state["threads"])))
    if env_flag("FEISHU_REQUIRE_MENTION") and not fc.bot_open_id(token):
        print("FEISHU_REQUIRE_MENTION is on but the bot's open_id is unknown")
        failures += 1
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
