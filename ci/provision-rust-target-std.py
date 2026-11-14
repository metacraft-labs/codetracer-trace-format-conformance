"""Give the declared Rust compiler the native gate's LLVM target standard library.

The Windows entry gate (tests/run_entry_coupled.py) builds the Rust producer
and runs the recorded-entry tests for `<arch>-pc-windows-gnullvm`, the target
that links with the provider's Clang. The declared `rustc` (repro.nim's
`rustc >=1.85`, realized from rust-lang's `rust-<ver>-<host>.tar.xz` under
REPRO_TOOL_PROVISIONING=tarball) carries only its host's standard library, and
it is not a rustup toolchain, so `rustup target add` has nothing to act on and
there is no rustup to run.

This script supplies the missing piece the way rustup would, without touching
the compiler's own prefix (a shared, content-addressed store path):

  1. it reads the declared compiler's `rustc -vV`;
  2. downloads rust-lang's `rust-std-<release>-<triple>.tar.gz` component for
     exactly that release, checked against the SHA-256 pinned below;
  3. refuses it unless the component was built from the compiler's own commit
     (a standard library is loadable only by the rustc that built it);
  4. extracts its `lib/rustlib/<triple>/` tree into a fresh directory, which
     is then a sysroot holding that one target;
  5. proves the compiler loads `std` from it; and
  6. exports `CARGO_TARGET_<TRIPLE>_RUSTFLAGS=--sysroot=<directory>` to the
     later steps through GITHUB_ENV.

Cargo applies `target.<triple>.rustflags` only to units compiled for that
target when `--target` is given, so build scripts and proc-macros keep
building for the host against the compiler's own sysroot. This is the
target-only sysroot that xargo and cargo-xbuild used.
https://doc.rust-lang.org/cargo/reference/config.html#targettriplerustflags
https://doc.rust-lang.org/cargo/reference/environment-variables.html#configuration-environment-variables

The catalog has no route for this yet: its `rust-std` package has Linux arms
only, and its arms are selected for the host, so naming it in `uses:` would
realize nothing (reprobuild repro_dsl_stdlib/packages/rust_std.nim). Nor does
the catalog's `rustup` package offer rustup here: its Windows tarball arm is
the same standalone x86_64-msvc Rust archive, with `cargo.exe` as its
executable, and only its Scoop arm installs a real rustup
(repro_dsl_stdlib/packages/rustup.nim).

Run it through `dev-exec`, so `rustc` is the declared compiler the gate and
Cargo later resolve the same way.
"""
from pathlib import Path, PurePosixPath
import argparse, hashlib, os, platform, shutil, subprocess, tarfile, tempfile, urllib.request

# One row per (release, target). The digests are rust-lang's own
# `<archive>.sha256` files, read 2026-11-14, equal to the `hash` rows of
# https://static.rust-lang.org/dist/channel-rust-1.94.0.toml. 1.94.0 is the
# release the catalog's Windows `rustc` tarball arm realizes (reprobuild
# v0.2.17, packages/rustc.nim). A compiler of another release is refused with
# the releases known here: add its rows from that release's channel file.
PINS = {
    ('1.94.0', 'x86_64-pc-windows-gnullvm'):
        '881b1cd467ac69dd8b0cf3e3f099bcc5051c64f5e7841ea7a018f31fc8899a87',
    ('1.94.0', 'aarch64-pc-windows-gnullvm'):
        '8afeb73b427d9039f44cddee301245a043f60220c44ba98581d3afec1f6e4406',
}

# The same host-to-target mapping as tests/run_entry_coupled.py.
WINDOWS_TARGETS = {'AMD64': 'x86_64-pc-windows-gnullvm', 'ARM64': 'aarch64-pc-windows-gnullvm'}

# Cargo reads the first of these that is set and ignores
# `target.<triple>.rustflags` altogether, which would drop the sysroot silently.
OVERRIDING_FLAGS = ('CARGO_ENCODED_RUSTFLAGS', 'RUSTFLAGS')


def rustflags_variable(triple):
    """Cargo's environment spelling of `target.<triple>.rustflags`."""
    return 'CARGO_TARGET_' + triple.upper().replace('-', '_').replace('.', '_') + '_RUSTFLAGS'


def compiler_identity(rustc):
    """The `key: value` rows of `rustc -vV`."""
    rows = {}
    for line in subprocess.check_output([rustc, '-vV'], text=True).splitlines()[1:]:
        key, separator, value = line.partition(': ')
        if separator:
            rows[key] = value.strip()
    for key in ('release', 'commit-hash', 'host'):
        if not rows.get(key):
            raise RuntimeError('rustc -vV of ' + rustc + ' names no ' + key)
    return rows


def download(url, expected_sha256, into):
    """Fetch `url` into the directory `into` and check its pinned digest."""
    archive = into / PurePosixPath(url).name
    request = urllib.request.Request(url, headers={'User-Agent': 'codetracer-trace-format-conformance-ci'})
    with urllib.request.urlopen(request, timeout=300) as response, archive.open('xb') as file:
        shutil.copyfileobj(response, file)
    with archive.open('rb') as file:
        actual = hashlib.file_digest(file, 'sha256').hexdigest()
    if actual != expected_sha256:
        raise RuntimeError(url + ' has SHA-256 ' + actual + ', pinned ' + expected_sha256)
    return archive


def extract_target_tree(archive, release, triple, destination):
    """Extract the component's `lib/rustlib/<triple>/` into `destination`.

    Returns the commit the component was built from. Only that subtree is
    written, under tarfile's `data` filter (no links out of the tree, no
    absolute or parent-relative names, no special files).
    """
    top = 'rust-std-' + release + '-' + triple
    component = top + '/rust-std-' + triple + '/'
    keep = component + 'lib/rustlib/' + triple + '/'
    with tarfile.open(archive, 'r:gz') as tar:
        commit = tar.extractfile(top + '/git-commit-hash').read().decode().strip()
        members = []
        for member in tar.getmembers():
            if member.name.startswith(keep):
                member.name = member.name[len(component):]
                members.append(member)
        # `std` for the crates, `test` for the harness `cargo test` links.
        names = [PurePosixPath(m.name).name for m in members]
        for crate in ('std', 'test'):
            if not any(n.startswith('lib' + crate + '-') and n.endswith('.rlib') for n in names):
                raise RuntimeError(archive.name + ' holds no lib' + crate + ' for ' + triple)
        tar.extractall(destination, members=members, filter='data')
    return commit


def probe(rustc, triple, sysroot, scratch):
    """Compile a crate that uses `std` for `triple` against `sysroot`.

    `--emit=metadata` resolves and loads `std` (and the crates it depends on)
    but links nothing, so this needs no target linker. A library built by
    another compiler fails here ("found crate `std` compiled by an
    incompatible version of rustc"); a sysroot rustc does not search fails
    with "can't find crate for `std`".
    """
    libdir = subprocess.check_output([rustc, '--print', 'target-libdir', '--target', triple,
                                      '--sysroot', str(sysroot)], text=True).strip()
    if Path(libdir).resolve() != (sysroot / 'lib' / 'rustlib' / triple / 'lib').resolve():
        raise RuntimeError('rustc resolves the ' + triple + ' library directory to ' + libdir)
    source = scratch / 'probe.rs'
    source.write_text('pub fn probe() -> usize { std::vec::Vec::<u8>::new().len() }\n')
    subprocess.run([rustc, '--edition=2021', '--crate-type=rlib', '--emit=metadata', '--target', triple,
                    '--sysroot', str(sysroot), '-o', str(scratch / 'libprobe.rmeta'), str(source)],
                   check=True)


def provision(rustc, triple, destination, pins=PINS):
    """Build the target sysroot at `destination` and return its rustflags row."""
    identity = compiler_identity(rustc)
    release, host = identity['release'], identity['host']
    if host == triple:
        raise RuntimeError('rustc is already a ' + triple + ' compiler; nothing to add')
    if host.split('-', 1)[0] != triple.split('-', 1)[0]:
        raise RuntimeError('rustc host ' + host + ' is not native to the ' + triple + ' gate host')
    pin = pins.get((release, triple))
    if pin is None:
        known = ', '.join(sorted(r + ' ' + t for r, t in pins))
        raise RuntimeError('No pinned rust-std component for Rust ' + release + ' / ' + triple +
                           ' (known: ' + known + '); add it to PINS in ci/provision-rust-target-std.py')
    destination = destination.absolute()
    # The value goes to Cargo as a space-separated flag list.
    if any(character.isspace() for character in str(destination)):
        raise RuntimeError('Target sysroot path contains whitespace: ' + str(destination))
    destination.mkdir(exist_ok=False)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=destination.name + '-scratch-') as scratch:
        scratch = Path(scratch)
        url = 'https://static.rust-lang.org/dist/rust-std-' + release + '-' + triple + '.tar.gz'
        archive = download(url, pin, scratch)
        commit = extract_target_tree(archive, release, triple, destination)
        if commit != identity['commit-hash']:
            raise RuntimeError('rust-std ' + release + ' ' + triple + ' was built from ' + commit +
                               ', rustc from ' + identity['commit-hash'])
        probe(rustc, triple, destination, scratch)
    return rustflags_variable(triple), '--sysroot=' + str(destination)


def append_environment(name, value):
    """Export `name=value` to the job's later steps."""
    if '\n' in value or '\r' in value:
        raise RuntimeError('Multi-line environment value')
    with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8', newline='\n') as file:
        file.write(name + '=' + value + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--destination', required=True, type=Path,
                        help='a new directory, directly under RUNNER_TEMP')
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('Actual Windows host required')
    triple = WINDOWS_TARGETS.get(platform.machine().upper())
    if triple is None:
        raise RuntimeError('Unsupported native Windows machine ' + platform.machine())
    destination = args.destination.absolute()
    if destination.parent.resolve() != Path(os.environ['RUNNER_TEMP']).resolve():
        raise RuntimeError('The target sysroot must be a direct child of RUNNER_TEMP')
    for name in OVERRIDING_FLAGS + (rustflags_variable(triple), 'RUSTC', 'RUSTC_WRAPPER'):
        # RUSTC / RUSTC_WRAPPER would make Cargo run a compiler other than the
        # `rustc` this script probed and the gate attests.
        if os.environ.get(name):
            raise RuntimeError(name + ' is already set; the ' + triple + ' sysroot would not be the one Cargo uses')
    rustc = shutil.which('rustc')
    if not rustc:
        raise RuntimeError('Declared rustc absent; run this script through dev-exec')
    name, value = provision(rustc, triple, destination)
    append_environment(name, value)
    print(name + '=' + value)


if __name__ == '__main__':
    main()
