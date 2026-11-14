#!/usr/bin/env bash
# The genuine-producer entry gate, run the way CI runs it.
#
# 1. ci/acquire-entry-runtime.sh builds the source-owned runtime constructor
#    and prepares the locked Nim peer (a fresh clone of
#    ../codetracer-trace-format-nim at the revision repro.lock pins) and its
#    runtime authority under .repro/build/entry-runtime.
# 2. tests/run_entry_coupled.py builds the Nim producer and the Rust producer
#    (examples/entry_cross_fixture.rs), has each read the other's images,
#    publishes the Nim images, and runs this repository's whole `cargo test`
#    inside the authenticated Nim environment with those images in
#    ENTRY_NIM_FIXTURE_ROOT.
#
# The constructor admits only a committed, clean checkout of this repository
# (it hashes the tracked sources it runs), and a Nim sibling at the pinned
# revision; `repro lock refresh` re-pins it.
set -euo pipefail
root=$(git rev-parse --show-toplevel)
cd "$root"
# This repository's dev shell points NIMBLE_DIR at its own seed for the Nim
# drivers. The peer's shell keeps a NIMBLE_DIR it inherits, and the runtime
# constructor requires the peer's own (<peer>/.nimble), so it must not leak in.
unset NIMBLE_DIR
state=$(mktemp -d "${TMPDIR:-/tmp}/conformance-entry-gate.XXXXXX")
trap 'rm -rf "$state"' EXIT
: >"$state/env"
: >"$state/path"
GITHUB_WORKSPACE=$root GITHUB_SHA=$(git rev-parse HEAD) \
  GITHUB_ENV=$state/env GITHUB_PATH=$state/path \
  bash ci/acquire-entry-runtime.sh
while IFS='=' read -r name value; do
  case "$name" in
    ENTRY_NIM_RUNTIME_AUTHORITY | ENTRY_NIM_ENVIRONMENT_ROOT) export "$name=$value" ;;
    *) echo "entry-gate: unexpected runtime variable $name" >&2; exit 1 ;;
  esac
done <"$state/env"
prefix=""
while IFS= read -r dir; do prefix="${prefix:+$prefix:}$dir"; done <"$state/path"
export PATH="$prefix:$PATH"
: "${ENTRY_NIM_RUNTIME_AUTHORITY:?the runtime constructor wrote no authority}"
: "${ENTRY_NIM_ENVIRONMENT_ROOT:?the runtime constructor wrote no peer}"
python3 tests/run_entry_coupled.py --nim-environment-root "$ENTRY_NIM_ENVIRONMENT_ROOT" \
  --direnv "$(command -v direnv)" --unix-runtime-authority "$ENTRY_NIM_RUNTIME_AUTHORITY"
