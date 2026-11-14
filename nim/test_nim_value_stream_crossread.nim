## M24a-2 cross-read driver: prove a NEW Nim-written production bundle's
## `values.dat` is read correctly by the canonical RUST `ValueStreamReader`.
##
## Steps:
##   1. Build + run the fixture generator
##      (`gen_value_stream_crossread_fixture.nim`) to write a real production
##      `.ct` bundle (SPEC-canonical values.dat/values.idx, has_value_stream
##      flag set) plus a sidecar of the decoded per-step values.
##   2. Run this repository's Rust half
##      (`tests/nim_value_stream_crossread.rs`),
##      pointing it at the bundle + sidecar via env vars. That test opens the
##      Nim bundle with the Rust `ValueStreamReader` and asserts the decoded
##      per-step `(name_id, CBOR bytes)` pairs equal the sidecar — the
##      load-bearing byte-compatibility proof (incl. the parallel-index
##      invariant: record N ↔ step N, empty for value-less steps).
##
## The Nim library is `../codetracer-trace-format-nim`, the Rust readers come
## from `../codetracer-trace-format` (this repository's Cargo path
## dependencies), and the plumbing is `crossread_harness.nim`.

import crossread_harness

streamCrossRead(
  label = "test_nim_value_stream_crossread",
  generator = "nim/gen_value_stream_crossread_fixture.nim",
  rustTest = "nim_value_stream_crossread",
  sidecarSuffix = ".values.txt",
  fixtureVar = "CT_NIM_VALUE_FIXTURE",
  sidecarVar = "CT_NIM_VALUE_FIXTURE_VALUES",
  failure = "Rust ValueStreamReader failed to cross-read the Nim values.dat")

echo "PASS: test_nim_value_stream_crossread (Nim values.dat read by Rust reader)"
