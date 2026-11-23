# A Feishu command pair for agents-pod

Two operator commands that hold every platform concern — app credentials, token
rotation, chat and thread paging, message flattening, reply addressing — so
core stays credential- and socket-free (ADRs [0024](../../docs/adr/0024-sources-pull-drain-and-cursor.md),
[0025](../../docs/adr/0025-outbox-operator-delivery-and-entry-files.md)).

| file | role | registered as |
|---|---|---|
| `feishu_drain.py` | Source: chats + threads → JSONL items + a cursor line | `agents-pod source add feishu --cmd <path>` |
| `feishu_deliver.py` | Deliverer: one Outbox entry → one Feishu reply, plus its image when the entry carries a path (#82) | `agents-pod deliver add feishu-reply --cmd <path>` |
| `feishu_client.py` | shared client: token cache, paging, message flattening | — |
| `.env.example` | the switchboard; copy to `.env` (0600) and fill | — |

Copy all four next to each other anywhere outside this repository — the pair is
meant to be *yours*, not core's. `chmod +x feishu_drain.py feishu_deliver.py`.
`state.json` and `.token.json` appear on first use; both are rebuilt if lost.

```bash
agents-pod adapter sync feishu --apply                     # into <store>/adapters/
cp ~/.agents/pods/adapters/.env.example ~/.agents/pods/adapters/.env && chmod 600 …
agents-pod source add feishu --cmd ~/.agents/pods/adapters/feishu_drain.py --timeout 60
agents-pod deliver add feishu-reply --cmd ~/.agents/pods/adapters/feishu_deliver.py --timeout 20
agents-pod doctor --probe      # the pair answers for itself; fix what it names
```

`adapter sync` copies from the **installed package** and records the hashes it
copied in `adapters.json`, which is what lets `doctor` tell you a copy fell a
release behind (ADR 0029) — a stale copy answers `--check` perfectly, so
behaviour alone cannot report it. A file you edited yourself is reported as a
local edit and kept; `--force` is the only way to replace it. Plain `cp` still
works; it just leaves nothing for `doctor` to compare against.

## One app, one drain, many groups

A tenant usually runs several groups, one per project. That needs **one**
drain and one Listener per group — the drain already walks every chat the bot
sits in, and every item carries its `chat_id` (ADR 0034):

```bash
agents-pod source add feishu --cmd ~/.agents/pods/adapters/feishu_drain.py --timeout 60
agents-pod listener add --source feishu --chat oc_aaaa --template ~/work/app-a/TASK.md --project ~/work/app-a
agents-pod listener add --source feishu --chat oc_bbbb --template ~/work/app-b/TASK.md --project ~/work/app-b
```

One cursor, one poll per pass, one Pod per group's message; a reply goes back to
the group it came from because the Delivery Address travels with the item. A
group no Listener names is **counted as `unrouted`** and printed by `doctor` —
the bot is reading it and nothing wants it, which is worth being told. Add
`--chat` to an existing Listener with `listener edit`, never `rm` + `add`: the
claims live in the record, and re-adding replays the overlap window as new work.

`FEISHU_CHATS` is a different axis: the adapter-level allowlist limits what the
**drain** reads at all (and gives each group its own cursor if you run one copy
per group). Use it for hard isolation, `--chat` for routing.

## The one thing this cannot do for you: the app

Feishu's console is a human surface. In order:

1. **Create a self-built app** (自建应用) and take `App ID` / `App Secret` from
   凭证与基础信息 into `.env`.
2. **Grant four tenant permissions**, then **publish a version** (权限变更 only
   takes effect with a new version; sensitive ones need an admin's approval).

   Every endpoint this pair calls, and the scope that admits it — the console
   names are the ones the permission picker shows, and the failure column is
   what the tick actually reports:

   | scope | console name | admits | measured failure without it |
   |---|---|---|---|
   | `im:chat:readonly` | 获取群组信息 | `GET /im/v1/chats` — the chats the bot is in | chat list comes back empty |
   | `im:message:readonly` | 获取单聊、群组消息 | the base read grant for `GET /im/v1/messages` | `code=230027` on read |
   | `im:message.group_msg` | 获取群组中所有消息 | group history — the base grant alone is **not** enough for a group | `code=230027` on read, with the base grant already on |
   | `im:message:send_as_bot` | 以应用的身份发消息 | `POST /im/v1/messages` and `POST /im/v1/messages/:id/reply` | send refused |
   | `im:resource` | 获取与上传图片或文件资源 | `POST /im/v1/images` — the upload behind an entry's `image` (#82) | refused with `code=99991672`, the console link in `ext` |

   Three of those four are "any one of a set" in the API docs, which matters
   when ticking boxes in the console:

   - read base: `im:message` · `im:message:readonly` · `im:message.history:readonly`
   - group history: `im:message.group_msg` — **required in addition**, app identity only
   - send: `im:message` · `im:message:send_as_bot` · `im:message:send`
   - chat list: `im:chat` · `im:chat:readonly` · `im:chat:read` · `im:chat.group_info:readonly`

   So the least-privilege set is those four; `im:message` replaces two of them
   (read base + send) and the set becomes three, at the price of also being
   allowed to send direct messages. `GET /bot/v3/info` (the bot's own id, for
   @-mention detection) needs no scope — only 机器人能力.

   Not scopes, but the same console and the same publish step: **机器人能力**
   must be on (应用能力 → 机器人), the version must be published, and the bot
   added to the group (`im:chat:readonly` cannot invent a membership).
3. **Add the bot to a group chat** — 群设置 → 群机器人 → 添加机器人. Create that
   group yourself, in the Feishu app: see the trap table below.
4. **Say one thing in the group**, then run the smoke test below.

## Swapping the app

A new app is a new identity. Three things on this machine still belong to the
old one, and only the first is in `.env`:

| what | why it matters | what to do |
|---|---|---|
| `.env` | the credentials | write the new `FEISHU_APP_ID` / `FEISHU_APP_SECRET`, then `chmod 600` |
| `.token.json` | the old app's `tenant_access_token`, valid 2 h — keyed by time, so it would keep answering as the old app | delete it, or let this version notice: the cache now records its `app_id` and a mismatch is a miss |
| `.bot.json` | the old app's bot `open_id`, used to spot an @-mention — a stale id makes `FEISHU_REQUIRE_MENTION=1` never match | same: cached per `app_id`, so a swap is a miss |

`state.json`'s `known_chats` survives a swap (chat ids are tenant-level, not
app-level), but a group the **new** bot was never added to will never yield an
item — re-add it and say one thing there.

After the swap: `agents-pod doctor --probe`; `token ok` + `N chat(s) visible` +
`read ok in <group>` is the whole handshake.

## Smoke test

```bash
cd ~/.agents/pods/adapters
env AGENTS_POD_SOURCE_ID=feishu AGENTS_POD_CURSOR= ./feishu_drain.py   # JSONL items, then {"cursor":"…"}
printf '%s' '{"to":"chat=oc_xxx;affinity=om_xxx","text":"通道自检"}' | ./feishu_deliver.py
printf '%s' '{"to":"chat=oc_xxx","text":"图片自检","image":"/tmp/shot.png"}' | ./feishu_deliver.py   # text, then the image (#82)
```

`exit 0` on both is the wiring. A drain that prints nothing **and** exits `0`
is not proof of health — it is the shape of "the pull is pointed at nothing",
which is why `sources.json` records `lastDrainItems` and why
`agents-pod doctor` prints a `?` line for a zero-yield drain (ADR 0027).

Both commands answer `--check` (exit `0` healthy, `1` not) with their own
diagnosis, including the console URL for a missing scope; `agents-pod doctor
--probe` runs those and quotes them.

## Measured traps

Facts observed while wiring this pair to a real tenant, in the order they bite:

| trap | what actually happens |
|---|---|
| **Send works while read is denied** | The bot can post into a group and cannot read a word of it, so a half-granted app looks alive. `im:message.group_msg` is the one that blocks intake (`230027`), and the error message carries its own console link. |
| **An app-created chat is invisible to humans** | `POST /im/v1/chats` succeeds (so does the local test above), but the group is created *by the app*: you do not see it in Feishu, and you cannot join it without another write scope. Create the group in the client and add the bot — that is the only shape a human can also type into. |
| **A new chat can take a moment to appear** | `GET /im/v1/chats` may return `items: []` for a chat that exists; a retry a few seconds later lists it. Treat one empty page as inconclusive before concluding the bot is in no group. |
| **p2p is not enumerable** | The chat list excludes p2p conversations, so a pull drain cannot see a direct message (ADR 0024). Group chats are the supported intake shape. |
| **`/im/v1/chats/:chat_id/link` is 404** | Not every endpoint in older notes exists on the tenant's version; do not build an invite flow on it. |
| **A version must be published** | Granting scopes without 版本管理与发布 changes nothing at runtime, and the refusal looks identical to "scope not granted". |
| **Chat anonymity hides history** | A group with 保密模式 or hidden history answers intake with `230050`/`231203` and no item ever arrives — a configuration fact, not a bug. |
| **Tokens and dedup windows are hours long** | `tenant_access_token` lives 2 h (`.token.json` caches it), so a 1-minute pass is cheap; the reply `uuid` window is 1 h, which is exactly what core's dead-lettering assumes (ADR 0025). |

## Events go to the log, messages go to the Inbox

A chat `system` event — an invite, a rename, a pin, a member join, the bot
being added — is something the conversation *did*, not something a human said.
It has no author to answer, so the drain does not turn it into an item. It
prints one line on stderr instead:

```
[feishu-drain] skip system event om_x100…443 in 徐福洋的小组
```

The rule is the same for any adapter that forwards platform events: **events →
the operator's log, messages → the Inbox.** A system event never spends a
pod wake-up.

Skipped events are *consumed*, not deferred: the cursor crosses them exactly as
it crosses a delivered message. So a pass whose only traffic was a rename
prints no item and still prints `{"cursor":"…"}` — that is a quiet chat, not
the "pointed at nothing" shape (zero items **and** no cursor) that `doctor`
reports (ADR 0027).

`msg_type: "system"` is excluded by content, not by identity: the existing
`sender_type == "app"` filter still drops the bot's own messages, and a human
message is still subject to `FEISHU_REQUIRE_MENTION`.

## Switchboard (`.env`, or the environment — the environment wins)

- `FEISHU_CHATS` — comma-separated `chat_id` allowlist; empty = every chat.
- `FEISHU_EXTRA_CHATS` — comma-separated `chat_id`s to inject even though the
  enumeration cannot see them: a p2p chat never appears in the chat list
  (ADR 0024), so an explicit id is the only way to opt one in. History-messages
  reads a p2p container fine given a p2p scope; injected chats carry a `p2p`
  mark and their own cursor.
- `FEISHU_REQUIRE_MENTION` — `1` reads only messages that @-mention the bot.
- `FEISHU_LOOKBACK_SECONDS` (3600) / `FEISHU_OVERLAP_SECONDS` (60) /
  `FEISHU_MAX_ITEMS` (40) — first-run window, re-read overlap behind the
  cursor, per-pass cap. The cap never loses messages: the cursor stops at the
  last item printed.

## Two independent commands, one platform

The pair shares a client file and nothing else. A different platform is a
second pair of commands and needs nothing from core; a second Feishu app needs
nothing from this directory but another `.env`.

## Limits the code meets, and the road not taken (push)

Facts from Feishu's own docs, recorded because they bound the *next* change
rather than this one. Every claim names the page it came from; none of them is
in the code yet.

**Frequency.** Error `230020` is the rate limit: **5 QPS to one group**, shared
by every bot in it, and 5 QPS to one user. A chat walk is one call per chat, so
the per-group cap — not the per-tenant one — is what a wide `FEISHU_CHATS`
meets first. ([message list](https://open.feishu.cn/document/server-docs/im-v1/message/list))

**Reply payloads** (what `feishu_deliver.py` may send): `content` is a
*serialized JSON string* whose shape follows `msg_type`; text is capped at
150 KB, cards and rich text at 30 KB; `uuid` is ≤50 characters with one hour of
idempotency; an image travels as `msg_type=image` after an upload that the
platform caps at 10 MB, non-empty, GIF ≤2000×2000, others ≤12000×12000
([reply message](https://open.feishu.cn/document/server-docs/im-v1/message/reply),
[upload image](https://open.feishu.cn/document/server-docs/im-v1/image/create)).
`reply_in_thread` refuses with `230071` (the group does not support
topic replies) and `230072` (aggregated messages). ([reply message](https://open.feishu.cn/document/server-docs/im-v1/message/reply))

**Message-list parameters** the drain does not pass yet: `sort_type`
(`ByCreateTimeAsc` default, fixed once paging starts), `page_size` 1–50
(default 20), `card_msg_content_type`, `only_thread_root_messages`,
`with_sender_name`. ([message list](https://open.feishu.cn/document/server-docs/im-v1/message/list))

**Push, if anyone ever wants it** (ADR 0024 leaves the door open; core stays
pull-only). The two transports share the at-least-once contract and the retry
schedule — 15 s, 5 min, 1 h, 6 h, at most four retries, ≈7 h — and differ in
everything else:

- *Webhook* needs a **public IPv4** URL, must echo `url_verification` within
  **1 s** before the URL can even be saved, and must return HTTP 200 within
  **3 s** (TCP connect 2 s + callback 3 s). A laptop that sleeps or changes
  network fails that ack, and the retry window is the only recovery.
- *Long connection* (outbound WebSocket) needs no public address — only
  outbound internet — and is **enterprise self-built apps only**. Success is the
  handler returning within **3 s** without throwing. The hard limits: **at most
  50 connections per app**, each initialized client being one connection, and
  delivery is **cluster mode with no broadcast** — with several clients of one
  app, only one randomly chosen client receives each message. A shim is
  therefore one connection, never a pool.

That last fact is why "push" here can only be a single resident process with a
file inbox behind it: any fan-out is the platform's, not ours.
