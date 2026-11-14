#!/usr/bin/env bash
set -euo pipefail
root=$(git rev-parse --show-toplevel)
test "$root" = "${GITHUB_WORKSPACE:?}"
test "$(git rev-parse HEAD)" = "${GITHUB_SHA:?}"
# The installed acquisition backend bootstraps the source-selected runtime roles.
bootstrap_nix=$(command -v nix)
test -x "$bootstrap_nix"
nix_package=$("$bootstrap_nix" build --no-link --print-out-paths --file ci/entry-runtime-profile.nix constructorNix.out)
[[ "$nix_package" =~ ^/nix/store/[a-z0-9]{32}-nix-[0-9.]+$ ]]
nix_image="$nix_package/bin/nix"
test -x "$nix_image"
expected_nix_drv=$("$nix_image" eval --raw --file ci/entry-runtime-profile.nix constructorNix.drvPath)
test "$("$nix_image" path-info --derivation "$nix_package")" = "$expected_nix_drv"
"$nix_image" store verify --recursive --no-trust "$nix_package"
nix_sha=$(sha256sum "$(readlink -f "$nix_image")" | cut -d' ' -f1)
python_package=$("$nix_image" build --no-link --print-out-paths --file ci/entry-runtime-profile.nix constructorPython.out)
[[ "$python_package" =~ ^/nix/store/[a-z0-9]{32}-python3-[0-9.]+$ ]]
test "$("$nix_image" path-info --derivation "$python_package")" = "$("$nix_image" eval --raw --file ci/entry-runtime-profile.nix constructorPython.drvPath)"
"$nix_image" store verify --recursive --no-trust "$python_package"
profile=$("$nix_image" build --no-link --print-out-paths --file ci/entry-runtime-profile.nix entryRuntime)
[[ "$profile" =~ ^/nix/store/[a-z0-9]{32}-prepare-entry-runtime$ ]]
expected_drv=$("$nix_image" eval --raw --file ci/entry-runtime-profile.nix entryRuntime.drvPath)
test "$("$nix_image" path-info --derivation "$profile")" = "$expected_drv"
"$nix_image" store verify --recursive --no-trust "$profile"
test "$(sha256sum "$(readlink -f "$nix_image")" | cut -d' ' -f1)" = "$nix_sha"
# Realise the locked Nim peer's development shell, online, before the
# constructor rebuilds it with `nix print-dev-env --offline`. Offline, Nix
# substitutes nothing: every path of that shell's closure missing from the
# store is built from source. The shell's pre-commit comes from git-hooks.nix's
# own nixpkgs, whose stdenv nothing else here puts in the store, so offline it
# is bootstrapped from the minimal seed. One of that bootstrap's sources is an
# on-the-fly repo.or.cz snapshot (tinycc
# cb41cbfe717e4c00d7bb70035cda5ee5f0ff9341.tar.gz), which repo.or.cz now
# answers with an anti-bot challenge page, so the fetch fails its hash
# (PR #1 run 43550388329: constant-refusal-40 at
# actual-locked-peer-cache-constructor). Online, all of it is substituted from
# the binary cache and only what no cache holds (the toolchain's Nim) builds.
# The constructor still runs offline and verifies what it uses; this only
# fills the store it reads.
peer_shell=$("$nix_image" build --no-link --print-out-paths --file ci/entry-runtime-profile.nix peerDevShell)
[[ "$peer_shell" =~ ^/nix/store/[a-z0-9]{32}-[^/]+$ ]]
test "$("$nix_image" path-info --derivation "$peer_shell")" = "$("$nix_image" eval --raw --file ci/entry-runtime-profile.nix peerDevShell.drvPath)"
test "$(sha256sum "$(readlink -f "$nix_image")" | cut -d' ' -f1)" = "$nix_sha"
# A failed stage of the runtime constructor appends its own error messages
# here, redacted: a Nix constructor's error records, any other stage's stderr
# tail (ci/prepare-cross-reader-runtime.py, publish_failure_diagnostics). They
# go to this log only, which masks the job's secrets; the uploaded receipts
# never hold them. Outside the repository and the runtime output, which are
# both audited.
nix_diagnostics=$(mktemp "${TMPDIR:-/tmp}/entry-runtime-nix-errors.XXXXXX")
trap 'rm -f "$nix_diagnostics"' EXIT
set +e
ENTRY_RUNTIME_NIX_DIAGNOSTICS=$nix_diagnostics \
  "$profile/bin/prepare-entry-runtime" --root "$root" --output "$root/.repro/build/entry-runtime"
runtime_status=$?
set -e
if ((runtime_status != 0)); then
  "$python_package/bin/python3" ci/report-runtime-constructor-failure.py "$root/.repro/build/entry-runtime" || echo "Public constructor diagnostic unavailable"
  if [[ -s $nix_diagnostics ]]; then
    echo "::group::Errors of the failed runtime constructor stages"
    cat "$nix_diagnostics"
    echo "::endgroup::"
    # The first line of each, as annotations on the run's summary page.
    grep -A1 -- '^--- .* error [0-9]* of ' "$nix_diagnostics" | grep -v -- '^--' | head -n 5 |
      while IFS= read -r line; do echo "::error title=Runtime constructor stage::${line//%/%25}"; done || true
  fi
  exit "$runtime_status"
fi
"$python_package/bin/python3" - "$root" "${GITHUB_ENV:?}" "${GITHUB_PATH:?}" "$python_package/bin" "$nix_package/bin" <<'PYTHON'
from pathlib import Path
import json, sys
root = Path(sys.argv[1])
base = root / '.repro/build/entry-runtime'
authority = base / 'runtime/peer-runtime-proof.json'
value = json.loads(authority.read_text())
owner = json.loads((base / 'runtime-owner.json').read_text())
peer = base / 'codetracer-trace-format-nim'
if base.resolve(strict=True) != base or peer.resolve(strict=True) != peer or owner.get('output') != str(base) or owner.get('root') != str(root):
    raise RuntimeError('Declared native producer output root differs')
if not all(value.get(k) is True for k in ['finished', 'success', 'finalGuards']):
    raise RuntimeError('Declared native runtime preparation incomplete')
role = Path(value['selectedDirenvImage']['lexical'])
if role.resolve(strict=True) != Path(value['selectedDirenvImage']['resolved']):
    raise RuntimeError('Declared native Direnv role changed')
# The original consumer reopens and verifies the authority and every image.
with Path(sys.argv[2]).open('a') as stream:
    for name, path in [('ENTRY_NIM_RUNTIME_AUTHORITY', authority),
                       ('ENTRY_NIM_ENVIRONMENT_ROOT', base / 'codetracer-trace-format-nim')]:
        if '\n' in str(path) or '\r' in str(path):
            raise RuntimeError('Invalid native runtime path')
        stream.write(name + '=' + str(path) + '\n')
with Path(sys.argv[3]).open('a') as stream:
    for path in [str(role.parent), sys.argv[4], sys.argv[5]]:
        if '\n' in path or '\r' in path:
            raise RuntimeError('Invalid native selected role path')
        stream.write(path + '\n')
PYTHON
