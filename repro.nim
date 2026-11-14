## Reprobuild project file for codetracer-trace-format-conformance.
##
## This repository holds the tests that need BOTH implementations of the
## CodeTracer trace format: the Nim one (codetracer-trace-format-nim) and the
## Rust one (codetracer-trace-format). Each cross-read test writes a container
## with the Nim writer and reads it back with a Rust reader; the fixture test
## reads the Rust implementation's shared trace-storage fixtures with the Nim
## parser. Neither library depends on this repository or on the other's tests,
## so this is the one place the two are compared.
##
## **Dependencies.** Both libraries are named in ``uses:`` below, which is what
## puts their pinned revisions in this repository's ``repro.lock``. The tests
## reach them BY PATH, as siblings: the Cargo path dependencies in
## ``Cargo.toml`` (``../codetracer-trace-format/<crate>``) and the Nim search
## path in ``config.nims`` (``../codetracer-trace-format-nim/src``).
##
## **Tool provisioning.** ``path``: the flake dev shell (``useFlakeDevShell()``)
## supplies the Nim toolchain both libraries are built with, the Rust
## toolchain, capnproto, libzstd and pkg-config, so ``repro run test`` and
## ``just test`` run with one set of tools.

import repro_project_dsl
import repro_dsl_stdlib/foreign_env

package codetracer_trace_format_conformance:
  devEnv:
    when not defined(windows):
      useFlakeDevShell()

    task "test", command = "just test",
      description = "Run the entry gate (the whole cargo suite inside the " &
        "authenticated Nim environment) and every Nim driver"
    task "lint", command = "just lint",
      description = "cargo fmt/clippy, nim check of every test module, " &
        "nixfmt"

  defaultToolProvisioning "path"

  uses:
    # The two implementations under test (see the module doc), named by
    # repository: these sibling edges put their pinned revisions in
    # repro.lock.
    "codetracer-trace-format"
    "codetracer-trace-format-nim"

    # The Rust halves: the floor both libraries' recipes declare, and
    # capnproto for ``codetracer_trace_format_capnp``'s build script.
    "rustc >=1.85"
    "cargo >=1.85"
    "capnp"

    # The Nim halves, compiled with the toolchain the Nim library declares.
    "nim >=2.2 <3.0"
    "nimble"
    when defined(macosx):
      "clang"
    else:
      "gcc >=12"

    # Both implementations link libzstd; the Nim halves find it through
    # pkg-config (``nim/runtime_nim_compile.nim``), which is not on the
    # Windows floor (zstd-sys builds libzstd from source there).
    "zstd"
    when not defined(windows):
      "pkg-config"
    "just"
