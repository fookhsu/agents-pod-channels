#!/usr/bin/env bash
# The guide's acceptance test: a fresh one-way channel, end to end, on
# npm-installed core — drain, tests, manifest, register, sync, routing, pass.
#
# The job never clones core (structural zero-touch: released core comes from
# npm), and it never edits this package: `manifest.json` here is a published
# surface, so the channel is written into a THROWAWAY copy of the tree.
#
# Every step below cites the README section it exercises. A step the guide does
# not teach is a missing section; a section this script never uses is
# redundancy — the two audit each other, and a red run means some change would
# have required touching core.
#
#   scripts/zero-touch.sh [core-version]
set -euo pipefail

version="${1:-latest}"
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

export AGENTS_POD_HOME="$work/home"
# The pass materializes a real pod; its wake-up must not launch an agent CLI.
export AGENTS_POD_PI_BIN=/usr/bin/false
project="$work/project"
tree="$work/tree"
mkdir -p "$project" "$tree"
cp -R "$repo/channels" "$tree/channels"
cp "$repo/manifest.json" "$repo/package.json" "$tree/"
rm -rf "$tree/channels/feishu/__pycache__"   # the move drops build residue

npm install -g --prefix "$work/prefix" "agents-pod@$version" >"$work/npm.log" 2>&1 || {
  cat "$work/npm.log"
  echo "error: could not install agents-pod@$version from npm"
  exit 1
}
export PATH="$work/prefix/bin:$PATH"
echo "zero-touch: agents-pod@$version from npm; the channel tree is $tree"

# ── §1 word first ────────────────────────────────────────────────────────────
# The boundary test is one line: has credentials, crosses the machine, is a
# Channel. The echo channel is the degenerate one-way case — no deliverer, no
# credentials, still a command pair that crosses the machine — so it is a
# Channel, and §1 is why the directory below holds commands and docs and no
# data.
channel="$tree/channels/echo"
mkdir -p "$channel"

# ── §3 scaffold rules ────────────────────────────────────────────────────────
# One flat directory under channels/<name>/. README.md and .env.example are
# channel bytes — shipped, hashed, synced. `.env` is never shipped, and a
# test_*.py lives in the repository without ever entering the operator's copy.
cat >"$channel/.env.example" <<'EOF'
# The echo channel's whole switchboard. An operator copies this to .env (chmod
# 600); .env itself never ships and a sync never overwrites one.
ECHO_GREETING=hello
EOF

cat >"$channel/README.md" <<'EOF'
# An echo channel

One-way: a drain that answers every pass with the one message it has. No
deliverer and no credentials — the degenerate one-way case of a Channel, and
still a command pair that crosses the machine.

## The pair

`echo_drain.sh` prints one JSONL item, then the cursor line. `--check` exits 0
with one line of diagnosis. `ECHO_GREETING` (see `.env.example`) is the
operator's only switch.

## Measured traps

Facts observed while wiring this channel, in the order they bite:

| trap | what actually happens |
|---|---|
| **The cursor line is read only last** | core takes the trailing `{"cursor":…}` line as the cursor and every other line as an item, so a cursor printed first is a record with no `id`: it is warned as a bad line, the item still arrives, and the cursor never moves — measured here: `scripts/zero-touch.sh` §2, asserted by `test_echo.py` |
| **A `test_*` file never reaches the operator** | sync takes runtime bytes only, so the copy carries no test to run and nothing says why — measured here: `scripts/zero-touch.sh` §4 asserts the copy's file list |
| **The item arrives as the pod's goal** | a template Listener spawns a pod per item and the rendered goal is the only place the body lands; nothing is visible in the store until a pass runs — measured here: `scripts/zero-touch.sh` §5 reads `agents-pod list --json` |
EOF

# ── §2 the three contracts ───────────────────────────────────────────────────
# Drain: one JSONL item per line, {"id", "body"}, the cursor line LAST. Check:
# `--check` prints a diagnosis and exits 0 or 1. Deliver is the other half of
# the contract; a one-way channel has none, which §1 admits as the degenerate
# case.
cat >"$channel/echo_drain.sh" <<'EOF'
#!/bin/sh
# One JSONL item, then the cursor line last — the drain contract, in full.
if [ "${1:-}" = "--check" ]; then
  printf 'echo ok: the one message is available\n'
  exit 0
fi
printf '{"id":"echo-1","body":"%s from the echo channel"}\n' "${ECHO_GREETING:-hello}"
printf '{"cursor":"echo-cursor-1"}\n'
EOF
chmod +x "$channel/echo_drain.sh"
"$channel/echo_drain.sh" --check >/dev/null   # the check contract, before core runs it

# ── §6 the test harness ──────────────────────────────────────────────────────
# stdlib unittest, offline, the drain run as a process — the same command CI's
# first lane runs for every channel in this package.
cat >"$channel/test_echo.py" <<'EOF'
"""The echo channel's contract tests: offline, stdlib only, as a process."""
import json
import os
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DRAIN = os.path.join(HERE, "echo_drain.sh")


def run(*args):
    env = dict(os.environ, AGENTS_POD_SOURCE_ID="echo", AGENTS_POD_CURSOR="")
    return subprocess.run([DRAIN, *args], capture_output=True, text=True, env=env, check=False)


class EchoDrainTest(unittest.TestCase):
    def test_one_item_then_the_cursor(self):
        done = run()
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(lines[0]["id"], "echo-1")
        self.assertIn("body", lines[0])
        self.assertEqual(lines[1], {"cursor": "echo-cursor-1"})

    def test_check_exits_zero(self):
        done = run("--check")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.strip())


if __name__ == "__main__":
    unittest.main()
EOF
python3 -m unittest discover -s "$channel" -p "test_*.py"

# ── §4 the manifest, the local sentinel, argument-free sync ──────────────────
# The manifest maps name → dir and declares the contract ranges; register reads
# it from the channel directory's parent, which is this layout. `--local` holds
# a self-built channel to the same record, sync and drift mechanics.
python3 - "$tree/manifest.json" <<'PY'
import json
import sys

path = sys.argv[1]
manifest = json.load(open(path))
manifest["channels"]["echo"] = {"dir": "channels/echo"}
json.dump(manifest, open(path, "w"), indent=2)
PY
agents-pod channel register echo --local --dir "$channel"
agents-pod channel sync            # dry run: the first look touches nothing
agents-pod channel sync --apply

# §4 doctor: no package to compare against, so the degraded line — with the
# recorded hashes still verified. Exit 1 is allowed only when something else on
# the machine is missing.
set +e
doctor=$(agents-pod doctor 2>&1)
code=$?
set -e
case "$code" in
  0 | 1) ;;
  *) echo "error: doctor exited $code"; printf '%s\n' "$doctor"; exit 1 ;;
esac
printf '%s\n' "$doctor" | grep -q 'echo' || {
  echo "error: doctor does not name the registered channel"
  printf '%s\n' "$doctor"
  exit 1
}
printf '%s\n' "$doctor" | grep -qi 'local' || {
  echo "error: doctor does not report the local sentinel"
  printf '%s\n' "$doctor"
  exit 1
}

# §3/§4 the record and the copy: README.md and .env.example shipped as bytes,
# `.env` never did, and the channel's test stayed in this repository.
copy=$(python3 - "$AGENTS_POD_HOME/.agents/pods/channels.json" <<'PY'
import json
import os
import sys

records = json.load(open(sys.argv[1]))
record = next((entry for entry in records if entry["name"] == "echo"), None)
assert record is not None, records
assert record.get("package") == "local", record
names = sorted(entry["name"] for entry in record["files"])
for required in (".env.example", "README.md", "echo_drain.sh"):
    assert required in names, (required, names)
assert ".env" not in names, names
assert "test_echo.py" not in names, names
assert os.stat(os.path.join(record["dir"], "echo_drain.sh")).st_mode & 0o111, "not executable"
print(record["dir"])
PY
)
echo "zero-touch: record ↔ copy at $copy"

# ── §5 registration and routing ──────────────────────────────────────────────
# `source add` registers the drain; a Listener binds it to a destination. A chat
# no Listener claims would be `unrouted` — a fact, not an error — and this
# Listener is what makes the item routed.
printf 'answer {{id}}: {{body}}\n' >"$project/template.md"
agents-pod source add echo --cmd "$copy/echo_drain.sh" --timeout 10
(cd "$project" && agents-pod listener add --source echo --template "$project/template.md")

# ── §2 + §5 the pass ─────────────────────────────────────────────────────────
# One reconcile pass drains the source, claims the item under the platform's id,
# commits the cursor after the claims, and materializes the template into a pod.
(cd "$project" && agents-pod reconcile)
python3 - "$AGENTS_POD_HOME/.agents/pods/pods.json" "$AGENTS_POD_HOME/.agents/pods/sources.json" <<'PY'
import json
import sys

pods = json.load(open(sys.argv[1]))
sources = json.load(open(sys.argv[2]))
goals = [pod["goal"] for pod in pods]
assert any("hello from the echo channel" in goal for goal in goals), goals
assert any("echo-1" in goal for goal in goals), goals
source = next(entry for entry in sources if entry["name"] == "echo")
assert source["cursor"] == "echo-cursor-1", source
print(f"pass: {len(pods)} pod(s) carry the item; the cursor committed after the claims")
PY

# ── §7 the measured-traps habit ──────────────────────────────────────────────
# Every channel README carries its own Measured traps table: measured, in the
# order they bite, every row naming its source. The shape is machine-checkable
# here; "this row names its source" stays a review rule, because it is a claim
# about where a fact came from, not a pattern. The exemplar is held to it too.
for readme in "$channel/README.md" "$repo/channels/feishu/README.md"; do
  grep -q '^## Measured traps' "$readme" || {
    echo "error: $readme has no Measured traps section"
    exit 1
  }
  grep -q '^| trap | what actually happens |' "$readme" || {
    echo "error: $readme's traps table does not state the two columns"
    exit 1
  }
  grep -q '^| \*\*' "$readme" || {
    echo "error: $readme's traps table has no row"
    exit 1
  }
done

echo "zero-touch: a one-way channel went from an empty directory to a pod carrying its first item"
