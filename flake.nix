{
  description = "Cross-implementation conformance tests of the CodeTracer trace format";

  inputs = {
    # The toolchain source of both implementations under test, so the Nim
    # and Rust halves of every test are built with the compilers each
    # implementation is built with.
    codetracer-toolchains.url = "github:metacraft-labs/nix-codetracer-toolchains";
    nixpkgs.follows = "codetracer-toolchains/nixpkgs";

    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };

    # The Nim packages the Nim implementation `requires` (`stew`, `results`,
    # and `unittest2`, which `stew` requires), seeded into a writable
    # `NIMBLE_DIR` by the dev shell, the way codetracer-trace-format-nim's
    # own shell seeds them, so a test compile resolves them offline.
    nim-stew = {
      url = "github:status-im/nim-stew";
      flake = false;
    };
    nim-results = {
      url = "github:arnetheduck/nim-results";
      flake = false;
    };
    nim-unittest2 = {
      url = "github:status-im/nim-unittest2";
      flake = false;
    };
    nim-packages-index = {
      url = "github:nim-lang/packages";
      flake = false;
    };
  };

  outputs =
    inputs@{ flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      perSystem =
        { pkgs, system, ... }:
        let
          toolchainsPkgs = inputs.codetracer-toolchains.packages.${system};

          # A ready-made `NIMBLE_DIR` payload. nimble accepts any
          # `pkgs2/<name>-<version>-<40 hex chars>` directory that carries the
          # package's `.nimble`; the checksum component is not verified when
          # the package is already present, so a stable placeholder is used
          # (the real pin is the flake input's revision).
          nimbleSeed = pkgs.runCommand "codetracer-trace-format-conformance-nimble-seed" { } ''
            mkdir -p $out/pkgs2

            seed() {
              local src="$1" name="$2" digits="$3"
              local ver
              ver=$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
                      "$src/$name.nimble" | head -n1)
              if [ -z "$ver" ]; then ver=0.0.1; fi
              local dst="$out/pkgs2/$name-$ver-$digits"
              cp -r "$src" "$dst"
              chmod -R u+w "$dst"
            }

            seed ${inputs.nim-stew}      stew      0000000000000000000000000000000000000001
            seed ${inputs.nim-results}   results   0000000000000000000000000000000000000002
            seed ${inputs.nim-unittest2} unittest2 0000000000000000000000000000000000000003

            cp ${inputs.nim-packages-index}/packages.json $out/packages_official.json
          '';
        in
        {
          devShells.default = pkgs.mkShell {
            packages = [
              # The Nim halves: the Nim implementation's compiler and the C
              # back-end `nim c` shells out to.
              toolchainsPkgs.nim-2_2
              toolchainsPkgs.nimble
              pkgs.gcc

              # The Rust halves: the Rust implementation's toolchain, and
              # capnproto for its `codetracer_trace_format_capnp` build script.
              toolchainsPkgs.rust-stable
              pkgs.capnproto

              # Both implementations link libzstd; the Nim halves resolve it
              # through pkg-config (`nim/runtime_nim_compile.nim`).
              pkgs.pkg-config
              pkgs.zstd
              pkgs.zstd.dev

              # Monitored launches require a non-SIP shell on macOS.
              pkgs.bash

              pkgs.just
              pkgs.nixfmt-rfc-style
              pkgs.git
            ];

            PKG_CONFIG_PATH = "${pkgs.zstd.dev}/lib/pkgconfig";

            # Seed a writable NIMBLE_DIR from the pinned sources above (nimble
            # writes nimbledata2.json, so the store copy cannot be used in
            # place). It goes into this repository's `.nimble/` when the shell
            # is entered from inside it, into the user cache otherwise; a
            # NIMBLE_DIR the caller already set is honoured.
            shellHook = ''
              if [ -z "''${NIMBLE_DIR:-}" ]; then
                ct_tfc_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
                if [ -n "$ct_tfc_root" ] && [ -f "$ct_tfc_root/nim/crossread_harness.nim" ]; then
                  export NIMBLE_DIR="$ct_tfc_root/.nimble"
                else
                  export NIMBLE_DIR="''${XDG_CACHE_HOME:-$HOME/.cache}/codetracer-trace-format-conformance/nimble"
                fi
                unset ct_tfc_root
              fi
              if [ ! -d "$NIMBLE_DIR/pkgs2" ]; then
                mkdir -p "$NIMBLE_DIR"
                cp -r ${nimbleSeed}/. "$NIMBLE_DIR/"
                chmod -R u+w "$NIMBLE_DIR"
              fi
            '';
          };
        };
    };
}
