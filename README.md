# agents-pod-channels

One directory per channel under `channels/`, one map from channel name to
directory in `manifest.json`. `npm i -g agents-pod-channels` puts the bytes on an
operator's machine; `agents-pod channel sync` keeps their copy from falling more
than a release behind.

This file is the authoring guide: seven sections, and every one of them is
exercised by the `zero-touch` job in `.github/workflows/ci.yml`
(`scripts/zero-touch.sh` cites the section each step comes from). A step the
guide does not teach is a missing section; a section the job never uses is
redundancy.

## 1. Word first: has credentials, crosses the machine, is a Channel

The vocabulary is the glossary's, not this guide's: read
[`CONTEXT.md` → `## Channels`](https://github.com/fookhsu/agents-pod/blob/master/CONTEXT.md#channels)
before the scaffold. The boundary test is one line — **has credentials, crosses
the machine, is a Channel.**

What the line rules out is as load-bearing as what it admits. A Terminal
Conversation and an Inbox move data that is already on the machine and hold no
platform credentials: machinery, not Channels, and neither belongs in this
repository. A channel with a drain and no deliverer is the **degenerate one-way
case**, not a different kind — it still crosses the machine.

## 2. The three contracts: drain, deliver, check

Core runs the first two (ADR [0024](https://github.com/fookhsu/agents-pod/blob/master/docs/adr/0024-sources-pull-drain-and-cursor.md),
[0025](https://github.com/fookhsu/agents-pod/blob/master/docs/adr/0025-outbox-operator-delivery-and-entry-files.md));
`doctor --probe` runs the third.

**Drain** — a Source command, once per reconcile pass. cwd is the project, the
environment is inherited, and core adds `AGENTS_POD_SOURCE_ID` and
`AGENTS_POD_CURSOR` (empty on the first run). stdout is JSONL: one item per
line, `{"id": …, "body": …}` plus an optional `title` and the routing fields
`chat_id` / `root_id` / `thread_id`. `id` **is the platform's** and it is the
dedup key, so it must survive a re-read. The **cursor line goes last**:
`{"cursor":"…"}`. A non-zero exit abandons that source's pass and leaves the
cursor where it was; a line that does not parse is warned and left unclaimed.
One line of ADR: core holds the cursor, never the queue.

**Deliver** — the operator's single delivery command, invoked once per outbox
entry **at wake-up exit**, the entry verbatim on stdin: `{"to": …, "text": …}`
and the optional `image` path. `to` is the Delivery Address
(`chat=…;affinity=…[;thread=…]`). Exit 0 acknowledges and core deletes the file;
any other exit leaves it for the next pass under the **same**
`AGENTS_POD_OUTBOX_UUID`. One line of ADR: the platform's one-hour idempotency
window under that uuid is the clock core's dead-lettering already keeps.

**Check** — both commands answer `--check`: print a self-check, exit **0**
healthy or **1** not, and name where to fix it (the console URL, the missing
scope, the expired token). `agents-pod doctor --probe` is the only mode that
runs operator code, and it quotes your lines. A drain that prints nothing and
exits 0 is healthy only when the platform had nothing to say: zero items with no
cursor is the "pointed at nothing" shape `doctor` reports as a `?` line (ADR
[0027](https://github.com/fookhsu/agents-pod/blob/master/docs/adr/0027-discovery-doctor-and-the-self-check.md)).

## 3. Scaffold rules: one flat directory, runtime bytes only

```
channels/<name>/
  README.md      the operator doc — ships as bytes, is hashed, is synced
  .env.example   the switchboard — ships; the operator copies it to .env (0600)
  <commands>     your executables, chmod +x
  test_*.py      your tests — stay in this repository, never enter a copy
```

- **Flat.** A subdirectory inside a channel directory is refused at register
  (exit 2). Sync copies the regular files at the top of the directory and skips
  everything else, so a subdirectory would be a file that silently never ships.
- **`.env` never ships and is never overwritten**, even if a package carries
  one: `.env`, `.token.json`, `.bot.json` and `state.json` are runtime-only, and
  a sync keeps whatever the operator has there.
- **`test_*.py` stay behind.** The tests live beside the bytes and run in CI;
  the operator's copy gets runtime bytes only, so there is no test file to run
  and nothing to explain. `__pycache__` and `*.pyc` are skipped the same way.
- **`package.json` `files` must cover `channels/`.** A channel outside the
  tarball registers from a checkout and fails for every operator.
  `node scripts/check-manifest.mjs` is what CI runs: every mapped directory
  exists, `files` covers it, `contracts` is declared.

## 4. Provenance mechanics: the manifest, one register, argument-free sync

`manifest.json` at the repository root is the map and the handshake:

```json
{
  "channels": { "feishu": { "dir": "channels/feishu" } },
  "contracts": { "drain": ">=1 <2", "deliver": ">=1 <2" }
}
```

Name and directory are not assumed equal. `contracts` is required: a manifest
without it is refused at register (exit 2), and core compares each range against
its own counter at register, at sync, and on **every pass** — a mismatch is
reported with a ✗ and a fix pointer, never a refusal, and while it lasts the
drain skips its pass and delivery freezes.

- **Register once.** `channel register <name> --package agents-pod-channels`
  records the name, the package, its version, the copy's directory and one
  sha256 per file in `channels.json`. Re-registering the same payload is a no-op
  that echoes the record; a different package under a registered name is a
  conflict naming the un-register path.
- **Sync takes no arguments, forever.** `channel sync` resolves the channel from
  that record, so an alias or a crontab line written today keeps working across
  package upgrades. It is a **dry run unless `--apply`**.
- **Doctor's three states, per file:** `same` (✓); `update`, a pristine copy one
  release behind (✗ naming `agents-pod channel sync --apply`); and `local-edit`,
  reported `?` and kept, because `--force` is the only way to replace a file
  someone edited. Behaviour cannot report the `update` state on its own: a stale
  copy answers `--check` perfectly, which is why provenance is hashes and not a
  probe.
- **The local sentinel, for a self-built channel.**
  `channel register <name> --local --dir <path>` gives your own directory the
  same record, sync and drift mechanics with `package: "local"`. Doctor prints
  `? local channel, no package to compare` and still verifies the recorded
  hashes.

## 5. Registration and routing: unrouted is a fact

Two ways in, one record either way:

```bash
agents-pod channel register mychat --package agents-pod-channels   # installed package
agents-pod channel register mychat --local --dir ~/work/mychat     # your own directory
```

A packaged channel resolves through `node_modules`: a directory that realpaths
outside any `node_modules` is refused with a reinstall pointer, so a working tree
can never pass for an installed release. A local channel is read where it is,
with its manifest in the parent directory — this repository's layout, which is
why `--local --dir channels/<name>` works from a checkout.

Routing is core's, and it is deliberately small:

```bash
agents-pod source add mychat --cmd <copy>/mychat_drain.py --timeout 60
agents-pod listener add --source mychat --chat <group-id> --template ~/work/app/TASK.md --project ~/work/app
```

One Source, one Listener per destination. A chat the drain reads and no Listener
claims is counted as **`unrouted`** and printed by `doctor`: the bot is reading
it and nothing wants it. **Unrouted is a fact, not an error** — the channel is
fine and the routing is incomplete, which are different problems with different
owners.

## 6. The test harness: stdlib unittest, offline, as a process

Python's standard library only — `unittest`, `subprocess`, `tempfile`. No pip
installs, no network, no credentials: CI's first lane runs the channel's tests
on every PR, and a dependency there would be a second package to trust.

```bash
python3 -m unittest discover -s channels/<name> -p "test_*.py"
```

Test the contract, not the code:

- **the drain as a process** — run the script with `AGENTS_POD_CURSOR` set, parse
  stdout, assert one item line per message with `id` and `body`, the cursor line
  last, and that `--check` exits 0;
- **the deliverer with an entry on stdin** — replace the one call in the shared
  client module, then assert exit 0, the reply addressed to the affinity message,
  and the same uuid on a retry.

The second half is the smoke lane: CI registers the channel `--local` against
`agents-pod@latest` **and** the release before it, syncs, and runs `doctor` — the
peer-range handshake proven on two cores. No job clones core; released core comes
from npm, which is what makes "zero touches to the core repository" structural
rather than a promise.

## 7. The measured-traps habit: every channel keeps its own table

Every channel README ends with a **Measured traps** table:

- **measured** — a fact observed while wiring the channel to a real tenant, not a
  restatement of the platform's documentation;
- **in the order they bite** — the first row is what you hit first;
- **every row names its source** — a console page, an error code, a platform doc
  URL, an ADR, or the CI step that measured it. A row that cannot name where it
  came from is a guess and does not belong.

The table stays with the bytes: it ships, it is hashed, it syncs, and it is what
a new maintainer reads before the code. `channels/feishu/README.md` is the
exemplar — the scope table with its measured failure codes, the app-created chat
that no human can see, p2p conversations that cannot be enumerated, and the
publish-version step whose absence looks identical to a missing scope.
