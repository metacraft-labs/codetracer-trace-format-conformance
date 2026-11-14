## M24a-1 cross-read driver: prove a NEW Nim-written production bundle's
## `steps.dat` is read correctly by the canonical RUST `StepStreamReader`.
##
## Steps:
##   1. Build + run the fixture generator
##      (`gen_step_stream_crossread_fixture.nim`) to write a real production
##      `.ct` bundle (SPEC-canonical steps.dat/steps.idx, has_step_stream flag
##      set) plus a sidecar of the decoded absolute global_line_index sequence.
##   2. Run this repository's Rust half
##      (`tests/nim_step_stream_crossread.rs`),
##      pointing it at the bundle + sidecar via env vars. That test opens the
##      Nim bundle with the Rust `StepStreamReader` and asserts the decoded GLI
##      sequence equals the sidecar — the load-bearing byte-compatibility proof.
##
## The Nim library is `../codetracer-trace-format-nim`, the Rust readers come
## from `../codetracer-trace-format` (this repository's Cargo path
## dependencies), and the plumbing is `crossread_harness.nim`.

import crossread_harness

streamCrossRead(
  label = "test_nim_step_stream_crossread",
  generator = "nim/gen_step_stream_crossread_fixture.nim",
  rustTest = "nim_step_stream_crossread",
  sidecarSuffix = ".steps-glis.txt",
  fixtureVar = "CT_NIM_STEP_FIXTURE",
  sidecarVar = "CT_NIM_STEP_FIXTURE_GLIS",
  failure = "Rust StepStreamReader failed to cross-read the Nim steps.dat")

echo "PASS: test_nim_step_stream_crossread (Nim steps.dat read by Rust reader)"
