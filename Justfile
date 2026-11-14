# Cross-implementation conformance tests of the CodeTracer trace format.
#
# Every recipe expects the two implementations checked out beside this
# repository: ../codetracer-trace-format (Rust) and
# ../codetracer-trace-format-nim (Nim). Run inside the dev shell
# (`nix develop`, or `repro run test`).

default:
    @just --list

alias t := test

# The full suite: the entry gate (which runs every Rust test here inside the
# authenticated Nim environment), then every Nim driver in nim/.
test: entry-gate test-nim test-scripts

# The genuine-producer entry gate and the whole `cargo test` (ci/entry-gate.sh).
# It admits only a committed, clean checkout whose repro.lock pins the Nim
# sibling's HEAD.
entry-gate:
    bash ci/entry-gate.sh

# Compile the Rust halves of the Nim drivers once, so each driver's
# `cargo test --test <name>` only runs.
build-rust:
    cargo test --locked --no-run

# Each `nim/test_*.nim`, compiled and run one at a time. A glob, not a list:
# a new test cannot be forgotten here. The cross-reads compile release, as the
# Nim implementation's own suite compiled them; the fixture test does not.
test-nim: build-rust
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p build/test-bin
    shopt -s nullglob
    tests=(nim/test_*.nim)
    (( ${#tests[@]} > 0 )) || { echo "no Nim tests found in nim/" >&2; exit 1; }
    for t in "${tests[@]}"; do
      stem="$(basename "$t" .nim)"
      flags=()
      case "$stem" in *_crossread) flags+=(-d:release) ;; esac
      echo "== $stem"
      nim c -r --hints:off "${flags[@]}" -o:"build/test-bin/$stem" "$t"
    done

# The guard on the runtime constructor's pinned Direnv plugin.
test-scripts:
    python3 -B tests/test_override_plugin_pin.py

# Formatting, clippy, `nim check` of every Nim module and the flake format.
lint:
    cargo fmt --check
    cargo clippy --locked --all-targets -- -D warnings
    for f in nim/*.nim; do nim check --hints:off "$f" || exit 1; done
    nixfmt --check flake.nix
