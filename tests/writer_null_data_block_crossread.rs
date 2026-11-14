//! A reopened append across the level-1/level-2 boundary of
//! `CTFS-Binary-Format.md` §4, held against the Nim implementation: the bytes
//! two reopened appends leave behind must read back, through the Nim reader
//! (`codetracer-trace-format-nim/tests/check_ctfs_container.nim`), exactly as
//! written. §5d makes agreement between implementations the acceptance
//! criterion for the format. The writer's refusals of damaged mappings, and the
//! Rust-only controls, are in codetracer-trace-format's
//! `codetracer_ctfs/tests/writer_null_data_block.rs`.
//!
//! No mocks: a real container written with the production `CtfsWriter`, sealed,
//! closed, reopened from disk and extended, read by the production Nim reader.

use codetracer_ctfs::{CtfsError, CtfsWriter};
use std::path::Path;
use tempfile::TempDir;

mod nim_adjudicator;
use nim_adjudicator::{nim_checker, run_nim_checker};

const BS: usize = 4096;

const MAX_ROOT_ENTRIES: u32 = 16;

fn deterministic_bytes(seed: u64, n: usize) -> Vec<u8> {
    let mut state = seed.wrapping_mul(0x9e37_79b9_7f4a_7c15) | 1;
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        out.push((state >> 33) as u8);
    }
    out
}

/// Write one stream per `(name, content)` pair and seal the container.
fn sealed_container(path: &Path, files: &[(&str, Vec<u8>)]) {
    let mut w = CtfsWriter::create(path, BS as u32, MAX_ROOT_ENTRIES).unwrap();
    for (name, content) in files {
        let h = w.add_file(name).unwrap();
        w.write(h, content).unwrap();
    }
    w.close().unwrap();
}

/// Reopen `path`, append `tail` to `name`, and seal it — the whole sequence a
/// consumer of a closed container performs, as one result. Nothing here pokes
/// at internals: `open_append` / `find_file` / `append` / `close` are the
/// crate's entire public reopen surface.
fn reopen_and_append(path: &Path, name: &str, tail: &[u8]) -> Result<(), CtfsError> {
    let mut w = CtfsWriter::open_append(path)?;
    let h = w.find_file(name).expect("find_file could not locate the stream");
    w.append(h, tail)?;
    w.close()
}

/// The cross-implementation half, and the reason it is on the *control* rather
/// than on a refusal: a refusal is a fact about one writer, whereas the bytes a
/// successful reopened append leaves behind are a claim about the **format**,
/// and §5d makes agreement between implementations the acceptance criterion for
/// that.
///
/// The fixture is deliberately not a pure function of its inputs. The container
/// is written, sealed, closed, reopened from disk — recovering `NextFreeBlock`
/// from the file length rather than from any in-memory state — and only then
/// extended across the level-1/level-2 boundary of §4. Its bytes therefore
/// depend on the on-disk state carried across the close, which is what makes an
/// agreement here worth asserting; a fixture built in one pass would be
/// satisfied by any writer that is merely self-consistent.
#[test]
fn the_nim_reader_agrees_with_what_a_reopened_append_wrote_across_a_level_boundary() {
    if nim_checker().is_none() {
        return;
    }
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("reopened.ct");

    let meta = deterministic_bytes(21, BS * 2);
    let head = deterministic_bytes(22, BS * 510);
    sealed_container(&path, &[("meta.dat", meta.clone()), ("big.dat", head.clone())]);

    // Two separate reopen-append cycles, so the second one inherits a mapping
    // the first one extended rather than one a single session built.
    let tail1 = deterministic_bytes(23, BS * 3);
    reopen_and_append(&path, "big.dat", &tail1).expect("first reopened append refused");
    let tail2 = deterministic_bytes(24, BS + 77);
    reopen_and_append(&path, "big.dat", &tail2).expect("second reopened append refused");

    let mut expected = head;
    expected.extend_from_slice(&tail1);
    expected.extend_from_slice(&tail2);

    let (out, ok) = run_nim_checker(dir.path(), &path, &[("meta.dat", &meta), ("big.dat", &expected)], &["absent.dat"]);
    assert!(
        ok,
        "the independent Nim reader does not agree with the container two reopened appends left \
         behind, so this crate's §4 walk across the level-1/level-2 boundary differs from the \
         other implementation's rather than merely being self-consistent:\n{out}"
    );
}
