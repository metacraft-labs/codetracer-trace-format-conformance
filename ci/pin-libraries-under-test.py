"""Pin repro.lock to the library checkouts this CI run is testing.

The entry gate admits only the Nim peer at the revision repro.lock pins. When
this suite runs as a gate of a library's change (conformance.yml under
`workflow_call`), the library checkout is that change, which this
repository's committed lock cannot name yet. This rewrites the revision (and
its integrity) of each library entry to the HEAD of the sibling checkout, and
nothing else, so the gate tests exactly the commit under review. CI commits
the result locally, because the runtime constructor admits only a committed
checkout; the commit is never pushed.

Usage: python3 ci/pin-libraries-under-test.py   (from the repository root)
Prints the entries it changed; exits 0 whether or not anything changed.
"""
import re
import subprocess
import sys
from pathlib import Path

LIBRARIES = ('codetracer-trace-format', 'codetracer-trace-format-nim')


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    lock_path = root / 'repro.lock'
    text = lock_path.read_text()
    for name in LIBRARIES:
        head = subprocess.check_output(['git', '-C', str(root.parent / name), 'rev-parse', 'HEAD'], text=True).strip()
        if not re.fullmatch(r'[0-9a-f]{40}', head):
            raise SystemExit(f'{name}: HEAD {head!r} is not a commit id')
        # One `{ name = "<name>", coord_kind = "vcs", ... }` entry per sibling.
        pattern = re.compile(r'(\{ name = "' + re.escape(name) + r'", coord_kind = "vcs",[^}]*?revision = ")([0-9a-f]{40})(", integrity = "git-sha1:)([0-9a-f]{40})(")')
        matches = pattern.findall(text)
        if len(matches) != 1:
            raise SystemExit(f'{name}: expected exactly one locked entry, found {len(matches)}')
        if matches[0][1] != head:
            print(f'{name}: {matches[0][1]} -> {head}')
            text = pattern.sub(lambda m: m.group(1) + head + m.group(3) + head + m.group(5), text)
    lock_path.write_text(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
