## M24a-3 cross-read driver: prove a NEW Nim-written production bundle's
## `events.dat` is read correctly by the canonical RUST `IoEventStreamReader`.
##
## Steps:
##   1. Build + run the fixture generator
##      (`gen_io_event_stream_crossread_fixture.nim`) to write a real production
##      `.ct` bundle (SPEC-canonical events.dat/events.idx, has_io_event_stream
##      flag set) plus a sidecar of the decoded per-record I/O events.
##   2. Run this repository's Rust half
##      (`tests/nim_io_event_stream_crossread.rs`),
##      pointing it at the bundle + sidecar via env vars. That test opens the
##      Nim bundle with the Rust `IoEventStreamReader` and asserts the decoded
##      `(kind, step_id, metadata, content)` records equal the sidecar — the
##      load-bearing byte-compatibility proof.
##
## The Nim library is `../codetracer-trace-format-nim`, the Rust readers come
## from `../codetracer-trace-format` (this repository's Cargo path
## dependencies), and the plumbing is `crossread_harness.nim`.

import crossread_harness

streamCrossRead(
  label = "test_nim_io_event_stream_crossread",
  generator = "nim/gen_io_event_stream_crossread_fixture.nim",
  rustTest = "nim_io_event_stream_crossread",
  sidecarSuffix = ".events.txt",
  fixtureVar = "CT_NIM_IO_EVENT_FIXTURE",
  sidecarVar = "CT_NIM_IO_EVENT_FIXTURE_EVENTS",
  failure = "Rust IoEventStreamReader failed to cross-read the Nim events.dat")

echo "PASS: test_nim_io_event_stream_crossread (Nim events.dat read by Rust reader)"
