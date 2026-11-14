//! `ConcurrentCtfsWriter`'s `flush` on a partial block, held against the Nim
//! implementation: the container the Rust concurrent writer produces across a
//! flush at an unaligned offset is handed to the Nim reader
//! (`codetracer-trace-format-nim/tests/check_ctfs_container.nim`), which must
//! read back exactly what the writer was told to write. `CTFS-Binary-Format.md`
//! §5d makes agreement between implementations the acceptance criterion; the
//! Rust-only round trips of the same writer are in codetracer-trace-format's
//! `codetracer_ctfs/tests/concurrent_writer_flush.rs`.
//!
//! No mocks: a real container written by the production `ConcurrentCtfsWriter`
//! onto a real filesystem, read by the production Nim reader.

use codetracer_ctfs::ConcurrentCtfsWriter;
use std::path::Path;
use std::sync::Arc;
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

/// Write `chunks` to one stream, calling `flush` after each chunk, and seal the
/// container. Returns the concatenation the container is supposed to hold.
fn write_with_flush_after_each_chunk(path: &Path, name: &str, chunks: &[&[u8]]) -> Vec<u8> {
    let writer = ConcurrentCtfsWriter::create(path, BS as u32, MAX_ROOT_ENTRIES).unwrap();
    let mut fw = writer.add_file(name).unwrap();
    let mut expected = Vec::new();
    for chunk in chunks {
        fw.write(&writer, chunk).unwrap();
        fw.flush(&writer).unwrap();
        expected.extend_from_slice(chunk);
    }
    drop(fw);
    Arc::try_unwrap(writer).expect("no other Arc refs").close().unwrap();
    expected
}

/// The independent Nim reader must read back exactly what the concurrent
/// writer was told to write, across a flush at an unaligned offset.
#[test]
fn the_nim_reader_agrees_with_what_the_concurrent_writer_was_told_to_write() {
    if nim_checker().is_none() {
        return;
    }
    let dir = TempDir::new().unwrap();
    let path = dir.path().join("crossread-flush.ct");

    let head = deterministic_bytes(51, 100);
    let tail = deterministic_bytes(52, BS * 3 + 55);
    let expected = write_with_flush_after_each_chunk(&path, "stream.dat", &[&head, &tail]);

    let (out, ok) = run_nim_checker(dir.path(), &path, &[("stream.dat", &expected)], &["absent.dat"]);
    assert!(
        ok,
        "the independent Nim reader does not agree with what the concurrent writer was told to \
         write, so the container this crate produced is wrong rather than merely read back \
         consistently:\n{out}"
    );
}
