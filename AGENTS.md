# codetracer-trace-format-conformance — agent instructions

This repository holds the tests that compare the two implementations of the
CodeTracer trace format: the Rust one (`../codetracer-trace-format`) and the
Nim one (`../codetracer-trace-format-nim`). Read `README.md` for the test list.

## Rules

- **Only cross-implementation tests belong here.** A test that exercises one
  library alone belongs in that library. A test that needs both belongs here,
  never in either library: neither library may depend on the other's
  checkout for its tests, and the Nim library depends on nothing else at all.
- **Dependencies come from `repro.nim`.** Both libraries are named in its
  `uses:`; `repro ws lock` derives `repro.lock` from it. The tests reach the
  libraries as siblings, by path (`Cargo.toml`, `config.nims`).
- **No skips.** A missing prerequisite (no `cargo`, no sibling checkout) fails
  the test. A test that cannot make its proof must not pass.
- **Fixtures are fictional.** Containers are written by the tests themselves;
  shared fixtures come from the libraries.

## Layout

- `nim/test_*.nim` — Nim drivers whose Rust halves are `tests/nim_*_crossread.rs`.
  `just test-nim` runs every one of them (a glob, not a list).
- `nim/gen_*_fixture.nim`, `nim/crossread_harness.nim`,
  `nim/runtime_nim_compile.nim` — their generators and plumbing.
- `tests/*.rs` — the Rust-side comparisons (writer and reader parity through
  the `codetracer_trace_writer_nim` adapter, the C ABI parity check, the Nim
  adjudicator cross-reads in `tests/nim_adjudicator/`, and the entry-image
  readers). `tests/mixed_workload/` and `tests/whole_file/` are shared helpers.
- `tests/run_entry_coupled.py`, `tests/entry_coupled.nim`,
  `tests/publish_entry_fixtures.py`, `examples/entry_cross_fixture.rs` — the
  genuine-producer entry gate.
- `ci/` — the entry runtime constructor (`acquire-entry-runtime.sh`,
  `prepare-entry-runtime.py`, `prepare-cross-reader-runtime.py`,
  `generated-runtime-namespace.py`, `*-profile*.nix`), the Windows native
  provider, `entry-gate.sh` (the local and CI driver) and
  `pin-libraries-under-test.py`.

## Commands

```
nix develop --command just test   # the entry gate (whole cargo suite) + Nim drivers
nix develop --command just lint   # cargo fmt/clippy, nim check, nixfmt
```

## CI

`.github/workflows/conformance.yml` runs the suite here, and is also a
reusable workflow: each library's `conformance.yml` calls it, with that
library's commit under test in place of its sibling. Keep its sibling
selection in step with who calls it.
