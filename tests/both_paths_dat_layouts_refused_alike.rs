//! A `meta.dat` that sets both bit 4 (column-aware steps, `paths.dat` Layout
//! A) and bit 14 (the `paths.dat` line-count table) declares two record
//! layouts for one `paths.dat`. The Rust and the Nim reader refuse the same
//! such container, each naming both bits.
//!
//! Spec: `codetracer-trace-format-spec/internal-files.md` §"`paths.dat`
//! line-count table": "Bits 4 and 14 are mutually exclusive. ... Writers
//! MUST NOT set both; readers MUST reject a header that does."
//!
//! Neither writer will produce such a container, so each one is a real
//! recording — line-count table or column-aware, written by the Rust writer —
//! with the other bit set in its `meta.dat` and every other member copied
//! verbatim. Unmodified, both readers read it, which the control asserts. No
//! mocks.

use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard, PoisonError};

use codetracer_ctfs::{CompressionMethod, CtfsReader, CtfsWriter};
use codetracer_trace_reader::ctfs_reader::read_trace_from_ctfs;
use codetracer_trace_types::Line;
use codetracer_trace_writer::ctfs_writer::CtfsTraceWriter;
use codetracer_trace_writer::meta_dat::{FLAG_HAS_COLUMN_AWARE_STEPS, FLAG_HAS_LINE_COUNT_TABLE};
use codetracer_trace_writer_nim::NimTraceReaderHandle;

static NIM_TEST_LOCK: Mutex<()> = Mutex::new(());

fn nim_lock() -> MutexGuard<'static, ()> {
    NIM_TEST_LOCK.lock().unwrap_or_else(PoisonError::into_inner)
}

/// A two-step Rust recording at `dir/<program>.ct`, line-count or
/// column-aware.
fn rust_recording(dir: &Path, program: &str, column_aware: bool) -> PathBuf {
    use codetracer_trace_writer::abstract_trace_writer::AbstractTraceWriter;
    use codetracer_trace_writer::trace_writer::TraceWriter;
    let src = Path::new("/src/a.py");
    let mut w = CtfsTraceWriter::new(program, &[]);
    if column_aware {
        w.enable_column_aware_steps();
    } else {
        w.enable_line_count_table().expect("table");
    }
    TraceWriter::begin_writing_trace_events(&mut w, &dir.join(program)).expect("begin");
    if column_aware {
        w.register_path_with_line_lengths(src, &[5, 5, 5]);
    } else {
        w.register_path_with_line_count(src, 10).expect("counted path");
    }
    AbstractTraceWriter::register_step(&mut w, src, Line(2));
    AbstractTraceWriter::register_step(&mut w, src, Line(3));
    TraceWriter::finish_writing_trace_events(&mut w).expect("finish");
    dir.join(format!("{program}.ct"))
}

/// The container at `ct` rewritten with `extra` OR-ed into its `meta.dat`
/// flags (bytes 6..8 of the 12-byte header).
fn with_flags(ct: &Path, extra: u16, out: &Path) {
    let mut reader = CtfsReader::open(ct).expect("open");
    let mut w = CtfsWriter::create_in_memory(4096, 31, CompressionMethod::None).expect("create");
    for (member, mut data) in reader.members().expect("members") {
        if member == "meta.dat" {
            let flags = u16::from_le_bytes([data[6], data[7]]) | extra;
            data[6..8].copy_from_slice(&flags.to_le_bytes());
            assert_eq!(flags & 0x4010, 0x4010);
        }
        let h = w.add_file(&member).expect("add");
        w.write(h, &data).expect("write");
    }
    std::fs::write(out, w.finish_to_bytes().expect("finish")).expect("write file");
}

fn names_both_bits(reader: &str, refusal: &str) {
    assert!(
        refusal.contains("bit 4") && refusal.contains("bit 14"),
        "the {reader} refusal does not name bits 4 and 14: {refusal}"
    );
}

#[test]
fn both_readers_refuse_a_header_declaring_both_paths_dat_layouts() {
    let _g = nim_lock();
    let dir = tempfile::tempdir().unwrap();
    for (label, column_aware, extra) in [
        ("line_count", false, FLAG_HAS_COLUMN_AWARE_STEPS),
        ("column_aware", true, FLAG_HAS_LINE_COUNT_TABLE),
    ] {
        let sub = dir.path().join(label);
        std::fs::create_dir_all(&sub).unwrap();
        let ct = rust_recording(&sub, label, column_aware);
        // The control: as written, both read it.
        read_trace_from_ctfs(&ct).unwrap_or_else(|e| panic!("{label}: the Rust reader refused the recording: {e}"));
        NimTraceReaderHandle::open(ct.to_str().unwrap()).unwrap_or_else(|e| panic!("{label}: the Nim reader refused the recording: {e}"));

        let both = sub.join(format!("{label}-both.ct"));
        with_flags(&ct, extra, &both);
        let rust = read_trace_from_ctfs(&both).expect_err("the Rust reader read a header declaring both layouts");
        names_both_bits("Rust", &rust.to_string());
        let Err(nim) = NimTraceReaderHandle::open(both.to_str().unwrap()) else {
            panic!("{label}: the Nim reader read a header declaring both layouts");
        };
        names_both_bits("Nim", &nim.to_string());
    }
}
