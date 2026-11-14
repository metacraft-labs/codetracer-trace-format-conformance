//! Cargo's host and target triples, for `tests/c_abi_parity.rs`: its C host is
//! compiled with the same target compiler Cargo builds the Rust library for,
//! selected through `cc`'s documented discovery rather than a PATH guess.
use std::env;

fn main() {
    for key in ["HOST", "TARGET"] {
        println!("cargo:rustc-env=CT_PARITY_{key}={}", env::var(key).expect("Cargo target metadata"));
    }
}
