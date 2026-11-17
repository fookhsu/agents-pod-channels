#!/usr/bin/env bash
# The smoke lane: a released core, one channel, register → sync → doctor.
#
# No core checkout exists anywhere in this job. Core arrives from npm with a real
# `npm install -g` into a throwaway prefix — the installed layout an operator
# has, without mutating the runner — which is what makes "zero touches to the
# core repository" structural rather than a promise: there is no clone to touch.
#
# The pair's own `--check` needs a live tenant (credentials, network). The first
# lane proves the contract offline; this lane proves the package surface on a
# released core.
#
#   scripts/smoke.sh <core-version>
set -euo pipefail

version="${1:?usage: scripts/smoke.sh <core-version>}"
repo=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

export AGENTS_POD_HOME="$work/home"
npm install -g --prefix "$work/prefix" "agents-pod@$version" >"$work/npm.log" 2>&1 || {
  cat "$work/npm.log"
  echo "error: could not install agents-pod@$version from npm"
  exit 1
}
export PATH="$work/prefix/bin:$PATH"
agents-pod --help >/dev/null   # a core that cannot print its own table is not a core
echo "smoke: agents-pod@$version from npm"

channel="$repo/channels/feishu"

# §4/§5: register once. The local sentinel anchors the record to this directory,
# so the lane proves register → sync → doctor without a published package.
agents-pod channel register feishu --local --dir "$channel"

# §4: sync is a dry run unless --apply, and takes no channel name.
agents-pod channel sync
agents-pod channel sync --apply

# §4: doctor's three states. A local channel has no package to compare against,
# so its line is the degraded one; the recorded hashes are still verified.
# Doctor exits 1 when something else on the machine is missing (a store with a
# channel and no Source has no deliverer either), so the assertion is about the
# channel's line, not about doctor's exit code being 0.
set +e
doctor=$(agents-pod doctor 2>&1)
code=$?
set -e
case "$code" in
  0 | 1) ;;
  *) echo "error: doctor exited $code"; printf '%s\n' "$doctor"; exit 1 ;;
esac
printf '%s\n' "$doctor" | grep -q feishu || {
  echo "error: doctor does not name the channel it was registered for"
  printf '%s\n' "$doctor"
  exit 1
}

# §3: the scaffold rules hold on the record and on the copy. README.md and
# .env.example ship as bytes, `.env` never ships, and test_* never enters the
# operator's copy.
python3 - "$AGENTS_POD_HOME/.agents/pods/channels.json" <<'PY'
import json
import os
import sys

records = json.load(open(sys.argv[1]))
record = next((entry for entry in records if entry["name"] == "feishu"), None)
assert record is not None, records
assert record.get("package") == "local", record
names = sorted(entry["name"] for entry in record["files"])
for required in (".env.example", "README.md", "feishu_client.py", "feishu_deliver.py", "feishu_drain.py"):
    assert required in names, (required, names)
assert ".env" not in names, names
assert not [name for name in names if name.startswith("test_")], names
copy = record["dir"]
assert os.path.isdir(copy), copy
for name in names:
    assert os.path.isfile(os.path.join(copy, name)), name
print(f"record: feishu → {copy} ({len(names)} files; no .env, no test_*)")
PY

echo "smoke: agents-pod@$version ok — register, sync and doctor answered for feishu"
