//! Cross-read: one Nim-written trace in five container geometries, read by the
//! Rust readers to the counts the Nim reader reported, which are the base
//! trace's.
//!
//! A reader takes a container's `BlockSize` and `MaxRootEntries` from its
//! header (`codetracer-trace-format-spec/ctfs-container.md` §1, "Container
//! Header"; a declared `0` is the auto-filled root, "Block 0 Layout"; both are
//! fixed when the container is created, "The Root Directory Is Fixed At
//! Creation"). The containers are the base trace the Nim writer produced and
//! its member-for-member rebuilds: `same` (the writer's own 4096 bytes and 31
//! slots, the control), `shift31` (an auto-filled root) and `root64` (64
//! slots), each with 31 one-byte fillers ahead of the trace so that its members
//! lie past slot 30, and `bs1024` and `bs2048`. A reader that scans a fixed 31
//! slots reads the shifted two as empty; one that resolves members with a
//! fixed 4096-byte block refuses the last two.
//!
//! For every container this requires the Rust readers to agree with the Nim
//! reader's record (the sidecar), and every container to read as the base:
//!
//! - `CtfsReader`: the block size, the slots it scans and the members it lists;
//! - the interning tables: the path and function counts;
//! - `StepStreamReader`: the step count.
//!
//! Driven by `nim/test_nim_declared_root_crossread.nim`, with two env vars:
//!
//! - `CT_NIM_DECLARED_ROOT_FIXTURE` — the base `<bundle>.ct`; the rebuilds are
//!   `same.ct`, `shift31.ct`, `root64.ct`, `bs1024.ct` and `bs2048.ct` beside it.
//! - `CT_NIM_DECLARED_ROOT_COUNTS` — the sidecar, one line per container:
//!   `<name> <BlockSize> <slots scanned> <members> <paths> <functions> <steps>`.
//!
//! When the env vars are absent (the Rust suite run on its own) the test is a
//! no-op, as every Rust half of a Nim driver is: there is no Nim fixture to
//! read. The Nim driver is what makes it load-bearing.

use codetracer_ctfs::CtfsReader;
use codetracer_trace_reader::interning_tables_reader::open_interning_tables;
use codetracer_trace_reader::step_stream_reader::open_step_stream;
use std::path::{Path, PathBuf};

/// The geometry and counts of one container, as one side reads it.
#[derive(Debug, PartialEq, Eq, Clone)]
struct Reading {
    block_size: u64,
    slots: u64,
    members: u64,
    paths: u64,
    functions: u64,
    steps: u64,
}

impl Reading {
    /// The three trace counts, which every container must share with the base.
    fn counts(&self) -> (u64, u64, u64) {
        (self.paths, self.functions, self.steps)
    }
}

/// What the Rust readers read from the container at `path`.
fn rust_reading(path: &Path) -> Reading {
    let shown = path.display();
    let reader = CtfsReader::open(path).unwrap_or_else(|e| panic!("{shown}: CtfsReader::open: {e}"));
    let tables = open_interning_tables(path)
        .unwrap_or_else(|e| panic!("{shown}: interning tables: {e}"))
        .unwrap_or_else(|| panic!("{shown}: no interning tables found"));
    let steps = open_step_stream(path)
        .unwrap_or_else(|e| panic!("{shown}: open_step_stream: {e}"))
        .unwrap_or_else(|| panic!("{shown}: no step stream found"));
    Reading {
        block_size: u64::from(reader.block_size()),
        slots: u64::from(reader.max_entries()),
        members: reader.list_files().len() as u64,
        paths: tables.path_count() as u64,
        functions: tables.func_count() as u64,
        steps: steps.count(),
    }
}

/// One sidecar line: a container's name and the Nim side's reading of it.
fn nim_reading(line: &str) -> (String, Reading) {
    let fields: Vec<&str> = line.split_whitespace().collect();
    assert_eq!(fields.len(), 7, "sidecar line {line:?} does not have 7 fields");
    let n = |i: usize| -> u64 { fields[i].parse().unwrap_or_else(|e| panic!("sidecar line {line:?}, field {i}: {e}")) };
    (
        fields[0].to_string(),
        Reading {
            block_size: n(1),
            slots: n(2),
            members: n(3),
            paths: n(4),
            functions: n(5),
            steps: n(6),
        },
    )
}

#[test]
fn nim_and_rust_read_every_declared_root_alike() {
    let fixture = match std::env::var("CT_NIM_DECLARED_ROOT_FIXTURE") {
        Ok(p) if !p.is_empty() => PathBuf::from(p),
        _ => {
            eprintln!(
                "nim_and_rust_read_every_declared_root_alike: CT_NIM_DECLARED_ROOT_FIXTURE \
                 unset — skipping (run via the Nim driver test for the cross-read proof)"
            );
            return;
        }
    };
    let sidecar = std::env::var("CT_NIM_DECLARED_ROOT_COUNTS").expect("CT_NIM_DECLARED_ROOT_FIXTURE set but CT_NIM_DECLARED_ROOT_COUNTS missing");
    let text = std::fs::read_to_string(&sidecar).expect("read the sidecar");
    let rows: Vec<(String, Reading)> = text.lines().filter(|l| !l.trim().is_empty()).map(nim_reading).collect();

    // The base first, then each rebuild, with the geometry each was built
    // with: (name, BlockSize, slots scanned). The auto-filled root of a
    // version-5 header at 4096 bytes is (4096 - 16) / 24 = 170 slots.
    let expected = [
        ("base", 4096, 31),
        ("same", 4096, 31),
        ("shift31", 4096, 170),
        ("root64", 4096, 64),
        ("bs1024", 1024, 31),
        ("bs2048", 2048, 31),
    ];
    let names: Vec<&str> = rows.iter().map(|(n, _)| n.as_str()).collect();
    let want_names: Vec<&str> = expected.iter().map(|(n, _, _)| *n).collect();
    assert_eq!(names, want_names, "the sidecar lists the containers in order");

    let base = &rows[0].1;
    assert!(
        base.paths >= 2 && base.functions >= 1 && base.steps >= 4,
        "the base trace is too small to tell an empty read apart: {base:?}"
    );
    let dir = fixture.parent().expect("the fixture has a directory");
    let mut failures = Vec::new();
    for ((name, nim), (_, block_size, slots)) in rows.iter().zip(expected.iter()) {
        let path = if name == "base" {
            fixture.clone()
        } else {
            dir.join(format!("{name}.ct"))
        };
        let rust = rust_reading(&path);
        println!("{name:8}: nim {nim:?}\n{:8}  rust {rust:?}", "");
        if rust != *nim {
            failures.push(format!("{name}: the Rust readers read {rust:?}, the Nim reader {nim:?}"));
        }
        if (rust.block_size, rust.slots) != (*block_size, *slots) {
            failures.push(format!(
                "{name}: geometry ({}, {}), not the ({block_size}, {slots}) it was built with",
                rust.block_size, rust.slots
            ));
        }
        for (side, reading) in [("Rust", &rust), ("Nim", nim)] {
            if reading.counts() != base.counts() {
                failures.push(format!(
                    "{name}: the {side} reader counts {:?}, the base trace {:?}",
                    reading.counts(),
                    base.counts()
                ));
            }
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
