# codetracer-trace-format-conformance

Cross-implementation conformance tests of the CodeTracer trace format.

The format has two implementations:

- [codetracer-trace-format](https://github.com/metacraft-labs/codetracer-trace-format),
  the Rust crates (readers, writers, the CTFS container), and
- [codetracer-trace-format-nim](https://github.com/metacraft-labs/codetracer-trace-format-nim),
  the Nim library (the multi-stream writer, the readers, `ct-print`).

Each library tests itself. This repository holds the tests that need both:
the ones that check one implementation against the other. It depends on both
libraries; neither library depends on it. The CI of each library runs this
suite against every change there, so a change on either side is held against
the other before it lands.

## What is checked

**Nim writes, Rust reads (`nim/` drivers with their Rust halves in `tests/`).**

| Test (`nim/`) | Rust half (`tests/`) | What must agree |
| --- | --- | --- |
| `test_nim_step_stream_crossread` | `nim_step_stream_crossread.rs` | A Nim-written `steps.dat`, decoded by the Rust `StepStreamReader`: the global line indices and the locations they name. |
| `test_nim_value_stream_crossread` | `nim_value_stream_crossread.rs` | A Nim-written `values.dat`, decoded by the Rust `ValueStreamReader`: every step's `(name_id, CBOR)` pairs, value-less steps included. |
| `test_nim_io_event_stream_crossread` | `nim_io_event_stream_crossread.rs` | A Nim-written `events.dat`, decoded by the Rust `IoEventStreamReader`: every record's kind, step, metadata and content. |
| `test_nim_step_map_crossread` | `nim_step_map_crossread.rs` | Every `step-map.ns` lookup answered by the Nim and the Rust reader, lookups of line 0 included. |
| `test_nim_reads_shared_trace_storage_fixtures` | — | The Nim parser reads the trace-storage fixtures the Rust implementation's tests read (`codetracer_ctfs/tests/fixtures/trace_storage`) to the same values. |

A driver writes a container with the Nim writer, records what the Nim reader
decodes from it, and runs its Rust half through `cargo test` with the
container and that record in environment variables.

**Both writers, both readers (`tests/*.rs`).** The Nim writer is driven
through the Rust adapter crate (`codetracer_trace_writer_nim`) or the Nim C
ABI, the Rust writer directly, and the outputs are compared byte for byte
or read by the other implementation's reader:

* writer comparisons — `writer_differential`, `whole_container_parity`,
  `container_parity_on_a_mixed_workload`, `compact_profile_cross_writer`,
  `*_cross_writer`, `a_function_keeps_the_version_it_was_declared_in`,
  `meta_dat_blocks_cross_implementation`, `compact_crossread`;
* reader comparisons — `*_cross_reader`, `*_refused_alike`,
  `call_args_survive_the_rust_reader`, `column_aware_split_read`,
  `empty_names_round_trip`, `bigint_is_readable_by_ct_print`,
  `interning_table_set_cross_reader`;
* the adapter's Nim writer, read back by the Rust readers —
  `assignments_reach_the_trace`, `drop_variables_reach_the_trace`,
  `a_value_resolves_to_the_type_it_was_given`,
  `call_arguments_belong_to_the_callee`,
  `values_staged_before_the_first_step`,
  `streaming_empty_struct_keeps_its_siblings`, and
  `recorded_entry_checked_binding` (a Nim-written container rewritten by the
  Rust `CtfsWriter` and refused by the Nim reader);
* the Nim reader as adjudicator of containers the Rust writers produced —
  `ctfs_crossread`, `concurrent_writer_flush_crossread`,
  `writer_null_data_block_crossread` (`tests/nim_adjudicator/`);
* the C ABI — `c_abi_parity` drives one C host against both libraries;
* the genuine-producer entry gate — `tests/run_entry_coupled.py` builds the
  Nim producer (`tests/entry_coupled.nim`) and the Rust producer
  (`examples/entry_cross_fixture.rs`), has each read the other's images, and
  `recorded_entry_identity_crossread` and `recorded_entry_sealed` read the Nim
  images with the Rust reader. Its runtime constructor
  (`ci/acquire-entry-runtime.sh`, `ci/prepare-*.py`) prepares and
  authenticates the locked Nim peer environment the whole suite runs in.

A missing prerequisite fails a test; nothing here skips.

## Running

Check the three repositories out side by side:

```
codetracer-trace-format/
codetracer-trace-format-nim/
codetracer-trace-format-conformance/
```

Then, in this repository:

```
nix develop --command just test
```

or `repro run test`. The entry gate admits only a committed, clean checkout
of this repository whose `repro.lock` pins the Nim sibling's `HEAD`
(`repro lock refresh` re-pins it); CI pins the libraries under test with
`ci/pin-libraries-under-test.py`. `just lint` runs `cargo fmt`, `clippy`, `nim check` over
every Nim module and `nixfmt`.

## License

MIT, like both implementations. See [LICENSE](LICENSE).
