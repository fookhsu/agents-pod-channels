#!/usr/bin/env python3
"""Feishu reply deliverer — the single agents-pod delivery command (ADR 0025).

The wrapper invokes this once per outbox entry, at wake-up exit. The entry
arrives verbatim on stdin:

    {"to": "chat=<chat_id>;affinity=<root_id>[;thread=<thread_id>]",
     "text": "…", "image": "/abs/path.png"}

`to` is the Delivery Address rendered by `{{reply_to}}` (ADR 0026). A reply goes
to the affinity message — inside its thread when `thread` is present — so the
human's answer inherits `root_id` and the next turn routes back to the same
Conversation. `AGENTS_POD_OUTBOX_UUID` is Feishu's one-hour reply idempotency
key: the same value is sent on every retry of the same entry.

`image` (#82) is optional. When present, the file is uploaded first
(`POST /im/v1/images`, scope `im:resource`, ≤10 MB — the platform's own cap)
and the image is sent as a second, `msg_type=image` message after the text,
under the same reply-first addressing. A relative path resolves against the
deliverer's cwd — the Pod's project. The image message derives its own
idempotency key (`image_uuid`), so a retry never re-posts either half.

Exit 0 acknowledges delivery (core deletes the entry); anything else leaves the
file for the next pass — which re-sends the text under the same uuid, so
nothing doubles. `--check` (ADR 0027) prints a self-check instead, and exits
0 healthy or 1 not.
"""

import json
import os
import sys

import feishu_client as fc

# Feishu text messages cap at 150 KB; a chat reply has no business being long.
TEXT_CAP = 30000


def parse_address(raw):
    """`chat=…;affinity=…[;thread=…]` → dict, tolerating spaces and order."""
    fields = {}
    for part in (raw or "").split(";"):
        key, _, value = part.partition("=")
        key = key.strip().lower()
        if key:
            fields[key] = value.strip()
    return fields


def deliver(token, fields, msg_type, content, uuid):
    """One message, reply-first: the affinity thread when there is one, else the chat.

    Text and image take turns here (#82). The uuid rides in the body — that is
    where the platform dedupes (a query-param uuid does not, measured on a
    live tenant) — so each message gets its own one-hour idempotency window.
    A failed affinity reply falls back to the chat: the words still arrive.
    """
    chat_id = fields.get("chat", "")
    affinity = fields.get("affinity", "")
    thread_id = fields.get("thread", "")
    body = {"msg_type": msg_type, "content": content}
    if uuid:
        body["uuid"] = uuid

    if affinity:
        try:
            payload = dict(body)
            if thread_id:
                payload["reply_in_thread"] = True
            fc.api("POST", "/im/v1/messages/%s/reply" % affinity, token=token, body=payload)
            return "reply:%s%s" % (affinity, ":thread" if thread_id else "")
        except fc.FeishuError as err:
            sys.stderr.write("[feishu-deliver] reply to %s failed (%s); trying the chat\n"
                             % (affinity, err))
            if not chat_id:
                raise
    if not chat_id:
        raise fc.FeishuError("/im/v1/messages", "no-address", "neither affinity nor chat")
    fc.api("POST", "/im/v1/messages", token=token,
           params={"receive_id_type": "chat_id"},
           body={"receive_id": chat_id, **body})
    return "chat:%s" % chat_id


def image_uuid(uuid):
    """A second idempotency key, derived not invented (#82).

    Text and image are two platform messages; one uuid for both would let the
    second send swallow the first. Deriving from the entry's own uuid keeps
    ADR 0025's retry story intact — a later pass re-derives the same key from
    the same entry. 50 characters is the platform cap, hence the slice.
    """
    if not uuid:
        return uuid
    return uuid[:46] + "-img"


def self_check():
    """`--check`: the operator command's own report (ADR 0027). Exit 0 healthy, 1 not."""
    fc.load_env()
    try:
        fc.tenant_token()
    except fc.FeishuError as err:
        print("no tenant token: %s" % err)
        print("  credentials live in .env beside this file: cp .env.example .env, then fill")
        return 1
    print("token ok (tenant_access_token)")

    address = "chat=oc_x;affinity=om_x;thread=t_x"
    parsed = parse_address(address)
    if parsed != {"chat": "oc_x", "affinity": "om_x", "thread": "t_x"}:
        print("Delivery Address parser broken: %r -> %r" % (address, parsed))
        return 1
    print("Delivery Address parser ok: chat=…;affinity=…[;thread=…]")

    # No scope-check endpoint exists: `im:message:send_as_bot` is proven by a
    # real send, and a probe must not post into someone's chat. The same goes
    # for the upload side (#82).
    print("send scope is only proven by a real send; the first reply verifies it")
    print("image upload (im:resource) is likewise proven only by an entry carrying `image`")
    return 0


def main():
    if "--check" in sys.argv[1:]:
        return self_check()
    fc.load_env()
    raw = sys.stdin.read()
    try:
        entry = json.loads(raw)
    except ValueError:
        sys.stderr.write("[feishu-deliver] entry is not JSON\n")
        return 1
    if not isinstance(entry, dict):
        sys.stderr.write("[feishu-deliver] entry is not a JSON object\n")
        return 1

    text = entry.get("text")
    address = entry.get("to")
    if not isinstance(text, str) or not text.strip():
        sys.stderr.write("[feishu-deliver] entry has no text\n")
        return 1
    if not isinstance(address, str) or not address.strip():
        sys.stderr.write("[feishu-deliver] entry has no Delivery Address\n")
        return 1
    image = entry.get("image")
    if image is not None and (not isinstance(image, str) or not image.strip()):
        sys.stderr.write("[feishu-deliver] image, when present, is a non-empty path\n")
        return 1
    if len(text) > TEXT_CAP:
        text = text[:TEXT_CAP] + "\n…(truncated)"

    fields = parse_address(address)
    if not fields.get("chat") and not fields.get("affinity"):
        sys.stderr.write("[feishu-deliver] unusable Delivery Address %r\n" % address)
        return 1

    uuid = (os.environ.get("AGENTS_POD_OUTBOX_UUID") or "").strip()
    try:
        token = fc.tenant_token()
        where = deliver(token, fields, "text", json.dumps({"text": text}, ensure_ascii=False), uuid)
    except fc.FeishuError as err:
        sys.stderr.write("[feishu-deliver] %s\n" % err)
        return 1
    sys.stdout.write("[feishu-deliver] delivered to %s\n" % where)

    if image is not None:
        path = os.path.abspath(image.strip())  # relative resolves against the pod's project
        try:
            key = fc.upload_image(token, path)
            where = deliver(token, fields, "image", json.dumps({"image_key": key}), image_uuid(uuid))
        except (fc.FeishuError, OSError) as err:
            sys.stderr.write("[feishu-deliver] image %s: %s\n" % (path, err))
            return 1
        sys.stdout.write("[feishu-deliver] image delivered to %s\n" % where)
    return 0


if __name__ == "__main__":
    sys.exit(main())
