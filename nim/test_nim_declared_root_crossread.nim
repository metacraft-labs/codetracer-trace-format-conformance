## Cross-read driver: the Nim reader and the Rust reader open the same trace
## in five container geometries and report the same counts.
##
## A reader takes a container's block size and root directory size from its
## header (`codetracer-trace-format-spec/ctfs-container.md` §1, "Container
## Header", "Block 0 Layout" and "The Root Directory Is Fixed At Creation").
## The generator (`gen_declared_root_crossread_fixture.nim`) writes one trace
## with the Nim writer and rebuilds it, member for member, as a control with
## the writer's own geometry (4096 bytes, 31 slots), an auto-filled root and a
## 64-slot root that both place the trace past slot 30, and 1024- and
## 2048-byte blocks; it records what the Nim reader reads from each. The Rust
## half (`tests/nim_declared_root_crossread.rs`) reads the same five with the
## Rust `CtfsReader`, interning-table reader and `StepStreamReader`, and
## requires the geometry, the member count and the path, function and step
## counts to equal the Nim reader's and the base trace's.
##
## The Nim library is `../codetracer-trace-format-nim`, the Rust readers come
## from `../codetracer-trace-format` (this repository's Cargo path
## dependencies), and the plumbing is `crossread_harness.nim`.

import crossread_harness

streamCrossRead(
  label = "test_nim_declared_root_crossread",
  generator = "nim/gen_declared_root_crossread_fixture.nim",
  rustTest = "nim_declared_root_crossread",
  sidecarSuffix = ".declared-root.txt",
  fixtureVar = "CT_NIM_DECLARED_ROOT_FIXTURE",
  sidecarVar = "CT_NIM_DECLARED_ROOT_COUNTS",
  failure = "the Nim and Rust readers read the five container geometries " &
    "differently")

echo "PASS: test_nim_declared_root_crossread (five geometries read alike by Nim and Rust)"
