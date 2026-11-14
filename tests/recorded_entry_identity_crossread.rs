//! The recorded-entry identity of containers the Nim implementation wrote,
//! read by the Rust `CtfsReader`: the genuine Nim producer's marked image
//! yields its entry call, the unmarked one none. The images come from the
//! entry gate (`tests/run_entry_coupled.py`), which hands their published
//! directory to this test through `ENTRY_NIM_FIXTURE_ROOT`. No mocks.
#[path = "common/entry_fixture_capture.rs"]
mod entry_fixture_capture;
use codetracer_ctfs::CtfsReader;
use codetracer_trace_reader::recorded_entry_reader::RecordedEntryReader;
use std::path::Path;

#[test]
fn genuine_nim_images_crossread() {
    let captured = entry_fixture_capture::capture();
    let base = captured.path();
    let mut marked = CtfsReader::open(&Path::new(&base).join("marked.ct")).unwrap();
    let entry = marked.recorded_entry().unwrap().unwrap();
    assert_eq!((entry.call_key, entry.function_id, entry.entry_step), (1, 1, 1));
    assert!(marked.is_owned_image());
    let mut unmarked = CtfsReader::open(&Path::new(&base).join("unmarked.ct")).unwrap();
    assert_eq!(unmarked.recorded_entry().unwrap(), None);
    assert!(!unmarked.is_owned_image());
}
