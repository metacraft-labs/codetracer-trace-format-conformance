//! Cross-implementation conformance tests of the CodeTracer trace format.
//!
//! This crate has no API. Its integration tests (`tests/*.rs`) are the Rust
//! halves of the cross-read tests: each reads a container written by the Nim
//! implementation (`codetracer-trace-format-nim`) with the Rust readers of
//! `codetracer-trace-format`, and compares the decode with what the Nim
//! reader decoded from the same bytes. The Nim halves in `nim/` write those
//! containers and run these tests; see the repository README.
