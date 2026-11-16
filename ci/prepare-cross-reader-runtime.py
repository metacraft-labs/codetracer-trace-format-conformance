#!/usr/bin/env python3
"""Admit the declared reader runtime and the exact locked peer's tracked envrc."""
import base64, hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tomllib
import time
import shutil
import re
import shlex
import importlib.util
import ast

sys.dont_write_bytecode = True
helper_path = Path(__file__).with_name('generated-runtime-namespace.py')
spec = importlib.util.spec_from_file_location('generated_runtime_namespace', helper_path)
helper_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper_module)
verify_generated_outputs = helper_module.verify_generated_outputs

# Where a failed stage's own error messages go, so CI can print them
# (ci/acquire-entry-runtime.sh): a Nix constructor's error records, or any
# other stage's stderr tail. Taken out of the environment before anything
# runs: no child sees it, and the environment-continuity guards below compare
# against the environment without it. The receipts never hold these bodies.
NIX_DIAGNOSTICS = os.environ.pop('ENTRY_RUNTIME_NIX_DIAGNOSTICS', None)
# Values that must never reach the diagnostics: every variable whose name says
# it carries a credential, and the base64 forms in which Git's HTTP
# authorization headers carry them. Taken from this process's environment
# while it is still whole, and again from a stage's own environment (the
# peer's activated shell) when that stage fails.
def _secret_forms(value):
    yield value
    # Each line of a multi-line value (a PEM key, say) on its own: the
    # diagnostics keep only the last lines of a stage's stderr, which can
    # start part-way through such a value, where the whole would not match.
    for line in value.splitlines():
        if len(line.strip()) >= 8:
            yield line.strip()
    for raw in (value, 'x-access-token:' + value):
        yield base64.b64encode(raw.encode()).decode().rstrip('=')
def secret_values(environment):
    return {form for name, value in environment.items()
            if re.search(r'TOKEN|SECRET|PASSW|AUTH|PRIVATE|CREDENTIAL|_KEY$|^KEY|^GIT_CONFIG_VALUE_', name.upper())
            and len(value) >= 8
            for form in _secret_forms(value)}
SECRET_VALUES = secret_values(os.environ)

arguments = sys.argv[1:]
peer_override = None
if len(arguments) == 9 and arguments[-2] == '--peer-root':
    peer_override = Path(arguments[-1]).resolve(strict=True)
    arguments = arguments[:-2]
root, receipts, python_profile, direnv_profile, just_profile, git, nix = map(Path, arguments)
root = root.resolve(strict=True)
receipts = receipts.resolve(strict=True)
proof = {'finished': False, 'success': False, 'scope': 'Runtime prerequisite only; original tests unexecuted', 'stages': []}
diagnostic_source_peer = None

receipt_fd = os.open(receipts / 'peer-runtime-proof.json', os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
receipt_stat = os.fstat(receipt_fd)
def save():
    current = (receipts / 'peer-runtime-proof.json').lstat()
    if (current.st_dev, current.st_ino, current.st_mode, current.st_nlink) != (receipt_stat.st_dev, receipt_stat.st_ino, receipt_stat.st_mode, 1):
        raise RuntimeError('Changed receipt authority')
    body = (json.dumps(proof, indent=2) + '\n').encode()
    os.lseek(receipt_fd, 0, os.SEEK_SET)
    os.ftruncate(receipt_fd, 0)
    os.write(receipt_fd, body)
    os.fsync(receipt_fd)

def image(path):
    lexical = path.absolute()
    resolved = lexical.resolve(strict=True)
    st = resolved.stat()
    if not stat.S_ISREG(st.st_mode):
        raise RuntimeError('Nonregular selected image')
    return {'lexical': str(lexical), 'resolved': str(resolved), 'dev': st.st_dev, 'ino': st.st_ino,
            'mode': stat.S_IMODE(st.st_mode), 'sha256': hashlib.sha256(resolved.read_bytes()).hexdigest()}

def namespace(path):
    result = []
    def walk(current, relative):
        st = current.lstat()
        mode = stat.S_IMODE(st.st_mode)
        common = [relative, st.st_dev, st.st_ino, mode, st.st_uid]
        if stat.S_ISLNK(st.st_mode):
            result.append(common + ['link', os.readlink(current)])
        elif stat.S_ISREG(st.st_mode):
            result.append(common + ['file', hashlib.sha256(current.read_bytes()).hexdigest()])
        elif stat.S_ISDIR(st.st_mode):
            result.append(common + ['dir'])
            for child in sorted(current.iterdir()):
                # Git metadata has its own HEAD/index/config authority. No source
                # or activation-code member is omitted from this physical census.
                if relative == '.' and child.name == '.git':
                    continue
                walk(child, child.name if relative == '.' else relative + '/' + child.name)
        else:
            raise RuntimeError('Special peer namespace member')
    walk(path, '.')
    return result

def metadata(path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return subprocess.check_output([str(git), '-C', str(path), *args], env=env)

def tracked(path):
    rows = metadata(path, 'ls-tree', '-rz', 'HEAD').split(b'\0')
    result = []
    for row in rows:
        if not row:
            continue
        header, name = row.split(b'\t', 1)
        mode, kind, oid = header.split()
        p = path / os.fsdecode(name)
        st = p.lstat()
        if mode == b'120000':
            if not stat.S_ISLNK(st.st_mode):
                raise RuntimeError('Changed tracked link kind')
            body = os.fsencode(os.readlink(p))
        elif mode in (b'100644', b'100755') and stat.S_ISREG(st.st_mode):
            if bool(st.st_mode & 0o111) != (mode == b'100755'):
                raise RuntimeError('Changed tracked execution parity')
            body = p.read_bytes()
        else:
            raise RuntimeError('Unsupported tracked member')
        actual = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest().encode()
        if actual != oid:
            raise RuntimeError('Changed tracked member bytes')
        result.append([os.fsdecode(name), mode.decode(), oid.decode()])
    if metadata(path, 'diff', '--cached', '--name-only', 'HEAD'):
        raise RuntimeError('Changed peer index')
    return result

# The peer's Python constructors whose RuntimeError literals a failed child's
# diagnostics are matched against. The current peer installs its hooks with a
# shell script (scripts/install-git-hooks.sh), so there are none.
PEER_PYTHON_CONSTRUCTORS = []
# `.pre-commit-config.yaml` is tracked source of the peer since its committed
# hook tier (codetracer-trace-format-nim 1169f52): its shell hook installs the
# committed config's hooks and generates no config. Only these two roots are
# written by activation.
GENERATED_ROOTS = ('.direnv', '.nimble')
HOOK_INSTALLER = 'scripts/install-git-hooks.sh'
HOOK_CONFIG = '.pre-commit-config.yaml'
HOOK_MARKER = '# git-hooks: generated by scripts/install-git-hooks.sh'
hook_transition = None
hook_directory_bindings = []
def hook_directory_identity(path, fd=None):
    st = path.lstat() if fd is None else os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode) or path.resolve(strict=True) != path:
        raise RuntimeError('Native hook directory ownership differs')
    return [st.st_dev, st.st_ino, st.st_mode, st.st_uid]

def declared_peer_config_continuity(peer, before):
    if any(hook_directory_identity(path) != identity or hook_directory_identity(path, fd) != identity for path, fd, identity in hook_directory_bindings):
        return False
    current = metadata(peer, 'config', '--null', '--show-origin', '--list')
    if current == before:
        return hook_transition is None or namespace(Path(hook_transition['path'])) == hook_transition['namespace']
    if hook_transition is None:
        return False
    def split(data):
        fields = data.split(b'\0')
        if fields[-1] != b'' or (len(fields) - 1) % 2:
            raise RuntimeError('Unsupported actual Git config inventory schema')
        keep, hooks = [], []
        for origin, entry in zip(fields[::2], fields[1::2]):
            key, sep, value = entry.partition(b'\n')
            if not sep:
                raise RuntimeError('Unsupported actual Git config entry schema')
            if key == b'core.hookspath':
                hooks.append((origin, value))
            else:
                keep.append((origin, entry))
        return keep, hooks
    old_rest, old_hooks = split(before)
    new_rest, new_hooks = split(current)
    expected = (b'file:.git/config', os.fsencode(hook_transition['path']))
    if old_rest != new_rest or old_hooks not in ([], [expected]) or new_hooks != [expected]:
        return False
    if metadata(peer, 'config', '--local', '--get', 'core.hooksPath').rstrip(b'\n') != expected[1]:
        return False
    common = Path(os.fsdecode(metadata(peer, 'rev-parse', '--path-format=absolute', '--git-common-dir').rstrip(b'\n')))
    hooks = common / 'hooks'
    return common == peer / '.git' and str(hooks) == hook_transition['path'] and namespace(hooks) == hook_transition['namespace']

def nix_failure_classes(text):
    """Boolean observations of fixed selected Nix 2.31.2 error formats only."""
    patterns = {
        'nixFileOpenReported': "opening file '[^\\n]*'",
        'nixShowUnsupportedInputReported': "cannot show unsupported input '[^\\n]*'",
        'nixPathStatusReported': "getting status of '[^\\n]*'",
        'nixUnsupportedInputReported': "cannot fetch unsupported input '[^\\n]*'",

        "nixIfdDisabledReported": r"cannot build '[^\n]*' during evaluation because the option 'allow-import-from-derivation' is disabled",
        "nixSubstitutionDisabledReported": r"dependency '[^\n]*' of '[^\n]*' does not exist, and substitution is disabled",
        "nixStoreWriteDeniedReported": r"cannot substitute path '[^\n]*' - no write access to the Nix store",
        "nixMissingDerivationReported": r"cannot build missing derivation '[^\n]*'",
        "nixInvalidStorePathReported": r"path '[^\n]*' is not valid",
        "nixHttp401Reported": r"unable to download '[^\n]*': HTTP error 401\b",
        "nixHttp403Reported": r"unable to download '[^\n]*': HTTP error 403\b",
        "nixHttp404Reported": r"unable to download '[^\n]*': HTTP error 404\b",
        "nixHttp429Reported": r"unable to download '[^\n]*': HTTP error 429\b",
    }
    return {name: re.search(pattern, text) is not None
            for name, pattern in patterns.items()}



ACTIVITIES = {
    100: "copyPath", 101: "fileTransfer", 102: "realise", 103: "copyPaths",
    104: "builds", 105: "build", 106: "optimiseStore", 107: "verifyPaths",
    108: "substitute", 109: "queryPathInfo", 110: "postBuildHook",
    111: "buildWaiting", 112: "fetchTree",
}
RESULTS = {
    100: "fileLinked", 101: "buildLogLine", 102: "untrustedPath",
    103: "corruptedPath", 104: "setPhase", 105: "progress",
    106: "setExpected", 107: "postBuildLogLine", 108: "fetchStatus",
}

def nix_structured_observation(text):
    if not isinstance(text, str):
        raise TypeError("Expected diagnostic text")
    result = {
        "jsonRecords": 0, "malformedRecords": 0, "errorMessages": 0,
        "errorsWithPosition": 0, "errorsWithTrace": 0,
        "activitiesObserved": [], "resultsObserved": [],
    }
    activities, results = set(), set()
    for line in text.splitlines():
        if not line.startswith("@nix "):
            continue
        try:
            value = json.loads(line[5:])
        except (json.JSONDecodeError, ValueError):
            result["malformedRecords"] += 1
            continue
        if not isinstance(value, dict):
            result["malformedRecords"] += 1
            continue
        result["jsonRecords"] += 1
        kind = value.get("action")
        code = value.get("type")
        if kind == "start" and type(code) is int and code in ACTIVITIES:
            activities.add(ACTIVITIES[code])
        elif kind == "result" and type(code) is int and code in RESULTS:
            results.add(RESULTS[code])
        elif kind == "msg" and type(value.get("level")) is int and value["level"] == 0:
            result["errorMessages"] += 1
            if (type(value.get("line")) is int and value["line"] > 0
                    and type(value.get("column")) is int and value["column"] > 0):
                result["errorsWithPosition"] += 1
            if isinstance(value.get("trace"), list) and value["trace"]:
                result["errorsWithTrace"] += 1
    result["activitiesObserved"] = sorted(activities)
    result["resultsObserved"] = sorted(results)
    return result

# Credential shapes redacted even when no variable names them: GitHub tokens,
# credentials embedded in URLs and HTTP authorization headers.
SECRET_PATTERNS = [
    (re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'), '***'),
    # Any userinfo in a URL, `user:password@` or a bare token `token@`.
    (re.compile(r'(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@'), r'\1***@'),
    (re.compile(r'(?i)\b(authorization:\s*(?:(?:basic|bearer|token)\s+)?)\S+'), r'\1***'),
    (re.compile(r'(?i)\b(access-tokens\s*=\s*)\S.*'), r'\1***'),
]
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')

def redact(text, environment=None):
    values = SECRET_VALUES | secret_values(environment or {})
    # Longest first, so a value is never left half-masked by one it contains.
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, '***')
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text

def publish_failure_diagnostics(role, exit_code, text, environment=None):
    """Append what names the cause of a failed stage, redacted and bounded, to
    NIX_DIAGNOSTICS for the CI log. For a Nix constructor: its error-level
    messages and, for each derivation they name, the last lines of that
    build's log. (With `--log-format internal-json` Nix leaves the log tail out
    of the error message and streams every build line as a separate record
    instead.) For any other stage -- the metadata writer, the reader build,
    Direnv activation -- or a Nix run that left no error record: the last
    lines of its stderr. Nothing else: not the rest of the build logs, not
    stdout. `environment` is the stage's own environment, whose credentials
    are redacted too."""
    if not NIX_DIAGNOSTICS:
        return
    messages = []
    build_of = {}     # activity id -> derivation path
    tails = {}        # activity id -> last build log lines
    for line in text.splitlines():
        if not line.startswith('@nix '):
            continue
        try:
            record = json.loads(line[5:])
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        action, fields = record.get('action'), record.get('fields')
        if action == 'msg' and record.get('level') == 0 and isinstance(record.get('msg'), str):
            messages.append(ANSI.sub('', record['msg']))
        elif (action == 'start' and record.get('type') == 105 and isinstance(fields, list)
              and fields and isinstance(fields[0], str)):
            build_of[record.get('id')] = fields[0]
        elif (action == 'result' and record.get('type') == 101 and isinstance(fields, list)
              and fields and isinstance(fields[0], str)):
            tail = tails.setdefault(record.get('id'), [])
            tail.append(ANSI.sub('', fields[0]))
            del tail[:-40]
    if messages:
        named = set(re.findall(r'/nix/store/[a-z0-9]{32}-[^\s\'"`]+\.drv', '\n'.join(messages)))
        body = ''.join('--- ' + role + ' error ' + str(n + 1) + ' of ' + str(len(messages)) + '\n'
                       + redact(m.rstrip('\n'), environment)[:8000] + '\n' for n, m in enumerate(messages))
        for activity, drv in build_of.items():
            if drv in named and tails.get(activity):
                body += ('--- ' + role + ': last ' + str(len(tails[activity])) + ' build log lines of '
                         + drv + '\n' + '\n'.join(tails[activity]) + '\n')
    else:
        # No structured error: the plain stderr tail is all there is.
        # The header has the shape of the one above, so acquire-entry-runtime.sh
        # annotates its first line the same way.
        plain = [l for l in text.splitlines() if not l.startswith('@nix ')][-40:]
        body = ('--- ' + role + ' error 1 of 1 (exit ' + str(exit_code) + '; last '
                + str(len(plain)) + ' stderr lines)\n'
                + redact(ANSI.sub('', '\n'.join(plain)), environment)[-8000:] + '\n')
    # 64 KiB for the whole file, across every stage that appends to it. Every
    # cut above is made after redaction, so none can split a secret and leave
    # part of it; this last pass covers the headers and build-log tails.
    with open(NIX_DIAGNOSTICS, 'a', encoding='utf-8') as stream:
        room = max(0, 65536 - stream.tell())
        stream.write(redact(body, environment)[:room])

def run_child(command, peer, role, child_env=None):
    child = subprocess.Popen(command, cwd=peer, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True, env=child_env)
    fields = Path('/proc/' + str(child.pid) + '/stat').read_text().rsplit(')', 1)[1].split()
    birth = int(fields[19])
    sid = os.getsid(child.pid)
    row = {'role': role, 'pid': child.pid, 'birth': birth,
           'sid': sid, 'exitCode': None, 'remaining': [], 'unknown': []}
    proof['stages'].append(row)
    save()
    stdout, stderr = child.communicate()
    row['exitCode'] = child.returncode
    # Direnv diagnostics can contain configuration values. Retain only nonsecret
    # outcome metadata, never the raw activation/configuration payload.
    row['stdoutBytes'] = len(stdout)
    row['stderrBytes'] = len(stderr)
    row['stderrSha256'] = hashlib.sha256(stderr).hexdigest()
    if child.returncode:
        # Diagnostics stay in memory. Emit only fixed classifications and
        # literal refusal prefixes from the already authenticated peer scripts.
        text = stderr.decode('utf-8', errors='replace')
        prefixes = []
        if diagnostic_source_peer is not None:
            # The peer's hook installer is a shell script with no Python
            # refusal literals; only Python constructors are scanned.
            for name in [n for n in PEER_PYTHON_CONSTRUCTORS if (diagnostic_source_peer / n).is_file()]:
                source_tree = ast.parse((diagnostic_source_peer / name).read_text())
                for node in ast.walk(source_tree):
                    if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                            and isinstance(node.exc.func, ast.Name) and node.exc.func.id == 'RuntimeError'
                            and node.exc.args):
                        continue
                    value = node.exc.args[0]
                    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
                        value = value.left
                    if isinstance(value, ast.Constant) and isinstance(value.value, str) and len(value.value) > 10:
                        if value.value in text:
                            prefixes.append({'source': name, 'literalRefusalPrefix': value.value})
        commands = {'bash', 'python3', 'git', 'nix', 'direnv', 'jq', 'curl', 'dirname',
                    'basename', 'readlink', 'sort', 'sha256sum', 'cut', 'head', 'sed',
                    'grep', 'tr', 'mkdir', 'cp', 'chmod', 'touch', 'printf', 'env'}
        missing = sorted({name for name in re.findall(r'([A-Za-z_][A-Za-z0-9_-]*): command not found', text)
                          if name in commands})
        row['nonsecretFailureClassification'] = {
            'sourceRefusalPrefixes': prefixes,
            'knownMissingCommands': missing,
            'classes': {name: marker in text.lower() for name, marker in {
                'permissionDenied': 'permission denied',
                'readOnlyFilesystem': 'read-only file system',
                'shellSyntaxError': 'syntax error',
                'commandNotFound': 'command not found',
                'unboundVariable': 'unbound variable',
                'cannotExecute': 'cannot execute',
                'blockedEnvrc': 'is blocked',
                'hashMismatch': 'hash mismatch',
                'fallback': 'falling back',
            }.items()},
            'scope': 'Classifier metadata only; no diagnostic/config/export bytes or hashes retained',
        }
        if role in {"actual-locked-peer-cache-constructor",
                    "actual-locked-peer-runtime-role-constructor"}:
            # JSON logging escapes messages. Decode only in memory so the existing
            # grounded predicates retain their behavior; never emit message bodies.
            fixed_classes = nix_failure_classes(text)
            for line in text.splitlines():
                if not line.startswith("@nix "):
                    continue
                try:
                    record = json.loads(line[5:])
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict) or record.get("action") != "msg":
                    continue
                for field in ("msg", "raw_msg"):
                    message = record.get(field)
                    if isinstance(message, str):
                        for name, matched in nix_failure_classes(message).items():
                            fixed_classes[name] = fixed_classes[name] or matched
            row['nonsecretFailureClassification']['classes'].update(fixed_classes)
            row['nonsecretFailureClassification']['nixStructuredObservation'] = (
                nix_structured_observation(text))
        # Every failed stage, Nix or not, names its cause in the CI log
        # (never in the receipt): the classification above says only which
        # fixed patterns matched.
        publish_failure_diagnostics(role, child.returncode, text, child_env)
    while True:
        remaining, unknown = [], []
        for proc in Path('/proc').iterdir():
            if not proc.name.isdecimal():
                continue
            try:
                if proc.stat().st_uid != os.getuid():
                    continue
                fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[3]) == sid and fields[0] != 'Z':
                    remaining.append({'pid': int(proc.name), 'birth': int(fields[19])})
            except FileNotFoundError:
                continue
            except (PermissionError, OSError, ValueError) as error:
                unknown.append({'pid': int(proc.name), 'errorType': type(error).__name__})
        row['remaining'], row['unknown'] = remaining, unknown
        save()
        if unknown:
            raise RuntimeError('Unknown authorization session census')
        if not remaining:
            break
        time.sleep(.1)
    if child.returncode:
        raise RuntimeError('Declared peer runtime operation refused')
    return stdout, stderr

held = None
metadata_runtime_trees = {}
generated_verified = False
generated_after = None
try:
    deps = tomllib.loads((root / 'repro.lock').read_text())['lock']['deps']
    matches = [d for d in deps if d['name'] == 'codetracer-trace-format-nim']
    if len(matches) != 1:
        raise RuntimeError('Missing or duplicate locked Nim peer')
    dep = matches[0]
    revision = dep['revision']
    # A v3 lock records no `path`; a sibling is at ../<name> by workspace convention.
    if dep.get('path', '../' + dep['name']) != '../codetracer-trace-format-nim' or dep['url'] != 'https://github.com/metacraft-labs/codetracer-trace-format-nim' or dep['integrity'] != 'git-sha1:' + revision:
        raise RuntimeError('Foreign locked Nim peer authority')
    peer = peer_override or root.parent / 'codetracer-trace-format-nim'
    if peer.is_symlink() or peer.resolve(strict=True) != peer:
        raise RuntimeError('Foreign peer namespace')
    if metadata(peer, 'rev-parse', 'HEAD').decode().strip() != revision:
        raise RuntimeError('Foreign peer revision')
    module = direnv_profile / 'share/trace-cross-reader/nix-direnvrc'
    config_file = direnv_profile / 'share/trace-cross-reader/config/direnvrc'
    expected_config = 'source ' + str(module.resolve(strict=True)) + '\n'
    if config_file.read_text() != expected_config:
        raise RuntimeError('Foreign source-owned Direnv configuration')
    if not module.resolve(strict=True).is_relative_to('/nix/store') or not config_file.resolve(strict=True).is_relative_to('/nix/store'):
        raise RuntimeError('Foreign immutable Direnv runtime constructor')
    images = [image(p) for p in (Path(sys.executable), git, nix, direnv_profile / 'bin/direnv', module, config_file, helper_path, direnv_profile / 'share/trace-cross-reader/flake-overrides.bash', just_profile / 'bin/just')]
    for role, output in [('python', python_profile), ('direnv', direnv_profile), ('just', just_profile)]:
        drv = json.loads((receipts / (role + '-derivation.json')).read_text())
        paths = [v['path'] for d in drv.values() for v in d['outputs'].values() if 'path' in v]
        normalized = [p if p.startswith('/nix/store/') else '/nix/store/' + p for p in paths]
        if normalized.count(str(output)) != 1:
            raise RuntimeError('Selected runtime output differs from actual constructor')
    envrc = peer / '.envrc'
    if not stat.S_ISREG(envrc.lstat().st_mode) or envrc.is_symlink():
        raise RuntimeError('Foreign peer envrc')
    source = tracked(peer)
    diagnostic_source_peer = peer
    config = metadata(peer, 'config', '--null', '--show-origin', '--list')
    # Reject every foreign/duplicate initial hook config before constructors or
    # activation. Preserve all configuration bytes only in task memory.
    fields = config.split(b'\0')
    if fields[-1] != b'' or (len(fields) - 1) % 2:
        raise RuntimeError('Unsupported actual Git config inventory schema')
    initial_hook_entries = [(origin, entry.partition(b'\n')[2])
            for origin, entry in zip(fields[::2], fields[1::2])
            if entry.partition(b'\n')[0] == b'core.hookspath']
    if initial_hook_entries not in ([], [(b'file:.git/config', os.fsencode(peer / '.git/hooks'))]):
        raise RuntimeError('Preexisting foreign native hook configuration refused')
    # Fresh CI admission is deliberately exclusive. Existing activation/cache
    # code must not execute merely because its namespace was inventoried.
    for generated in GENERATED_ROOTS:
        if (peer / generated).exists() or (peer / generated).is_symlink():
            raise RuntimeError('Preexisting CI generated runtime root refused')
    tracked_names = {row[0] for row in source}
    if HOOK_CONFIG not in tracked_names or HOOK_INSTALLER not in tracked_names:
        raise RuntimeError('Peer hook config or installer is not tracked source')
    config_path = peer / HOOK_CONFIG
    if config_path.is_symlink() or not stat.S_ISREG(config_path.lstat().st_mode):
        raise RuntimeError('Peer hook config is not an ordinary tracked file')
    peer_namespace = namespace(peer)
    input_lock = json.loads((peer / 'flake.lock').read_text())
    for name in input_lock['nodes'][input_lock['root']]['inputs']:
        candidates = [peer.parent / name]
        if name.endswith('-src'):
            candidates.append(peer.parent / name[:-4])
        if any(x.exists() or x.is_symlink() for x in candidates):
            raise RuntimeError('CI runtime requires the exact locked inputs, not sibling overrides')
    for key in ['NIX_FLAKE_OVERRIDE_INPUTS', 'NIX_FLAKE_OVERRIDE_FLAKES', 'NIX_FLAKE_OVERRIDE_SIBLINGS', 'NIX_FLAKE_OVERRIDE_SIBLINGS_ROOT']:
        if os.environ.get(key):
            raise RuntimeError('Foreign CI flake override selector')
    if (peer / '.env').exists() or (peer / '.env').is_symlink():
        raise RuntimeError('Undeclared CI dotenv activation input')
    if (peer.parent / 'direnv-nix-flake-overrides/plugin/flake-overrides.bash').exists():
        raise RuntimeError('CI runtime requires the envrc pinned published plugin')
    generated_roots = set(GENERATED_ROOTS)
    source_namespace = [row for row in peer_namespace if row[0].split('/')[0] not in generated_roots]
    proof['prospectiveGeneratedRoots'] = sorted(generated_roots)
    proof['ordinarySourceNamespace'] = source_namespace
    proof['generatedInputBefore'] = [row for row in peer_namespace if row[0].split('/')[0] in generated_roots]
    root_source = tracked(root)
    root_config = metadata(root, 'config', '--null', '--show-origin', '--list')
    environment_before = dict(os.environ)
    held = (peer, source, config, images, peer_namespace, root_source, root_config, environment_before)
    # Independent same-source print-dev-env supplies the exact expected cache
    # body and seed. It uses no private environment replacement or copied envrc.
    expected_profile_slot = receipts / 'expected-nim-env-profile'
    if expected_profile_slot.exists() or expected_profile_slot.is_symlink():
        raise RuntimeError('Occupied expected runtime profile output')
    expected_rc, _ = run_child([str(nix), '--log-format', 'internal-json', 'print-dev-env', '--profile', str(expected_profile_slot), '--offline', '--option', 'allow-import-from-derivation', 'false', '--no-write-lock-file', '.'], peer, 'actual-locked-peer-cache-constructor')
    expected_rc = expected_rc.rstrip(b'\n') + b'\n'
    expected_json, _ = run_child([str(nix), '--log-format', 'internal-json', 'print-dev-env', '--json', '--offline', '--option', 'allow-import-from-derivation', 'false', '--no-write-lock-file', '.'], peer, 'actual-locked-peer-runtime-role-constructor')
    expected_environment = json.loads(expected_json)
    variables = expected_environment['variables']
    if not isinstance(variables, dict) or variables['PATH']['type'] != 'exported' or not isinstance(variables['PATH']['value'], str):
        raise RuntimeError('Unsupported actual print-dev-env PATH schema')
    expected_roles = {}
    for role in ['nim', 'nimble', 'git', 'gcc', 'bash']:
        lexical = shutil.which(role, path=variables['PATH']['value'])
        if not lexical or not Path(lexical).resolve(strict=True).is_relative_to('/nix/store'):
            raise RuntimeError('Independently constructed locked runtime role absent')
        expected_roles[role] = image(Path(lexical))
    proof['expectedSelectedImages'] = expected_roles
    shell_hook = variables['shellHook']['value']
    if not isinstance(shell_hook, str):
        raise RuntimeError('Unsupported actual shellHook constructor schema')
    # The peer's shell hook installs the hooks its committed config asks for
    # with its tracked installer, run by the flake's pre-commit:
    #   ( cd "$root" && bash <store>-install-git-hooks.sh <store>/bin/pre-commit )
    # Authenticate both: the installer must be byte-identical to the tracked
    # script, and pre-commit an immutable store image.
    installs = [shlex.split(line) for line in shell_hook.splitlines() if 'install-git-hooks.sh' in line]
    if len(installs) != 1:
        raise RuntimeError('Actual native-hook installer absent or ambiguous')
    tokens = installs[0]
    script_positions = [i for i, token in enumerate(tokens)
                        if re.fullmatch(r'/nix/store/[a-z0-9]{32}-install-git-hooks\.sh', token)]
    if len(script_positions) != 1 or script_positions[0] + 1 >= len(tokens):
        raise RuntimeError('Actual native-hook installer script absent or ambiguous')
    installer_script = Path(tokens[script_positions[0]])
    if installer_script.read_bytes() != (peer / HOOK_INSTALLER).read_bytes():
        raise RuntimeError('Native-hook installer differs from the tracked peer constructor')
    hook_pre_commit = Path(tokens[script_positions[0] + 1])
    if not re.fullmatch(r'/nix/store/[a-z0-9]{32}-[^/]+/bin/pre-commit', str(hook_pre_commit)):
        raise RuntimeError('Actual native-hook pre-commit constructor role differs')
    installer_image = image(installer_script)
    hook_pre_commit_before = image(hook_pre_commit)
    images.append(installer_image)
    images.append(hook_pre_commit_before)
    proof['nativeHookConstructor'] = {'installer': installer_image,
                                     'preCommit': hook_pre_commit_before,
                                     'config': HOOK_CONFIG}
    expected_profile = expected_profile_slot.resolve(strict=True)
    archive_bytes, _ = run_child([str(nix), 'flake', 'archive', '--json', '--offline', '--no-write-lock-file', '.'], peer, 'actual-locked-peer-source-constructor')
    archive = json.loads(archive_bytes)
    expected_sources = set()
    def source_paths(node):
        if not isinstance(node, dict) or not isinstance(node.get('path'), str) or not node['path'].startswith('/nix/store/'):
            raise RuntimeError('Invalid actual source archive schema')
        expected_sources.add(node['path'])
        for child in node.get('inputs', {}).values():
            source_paths(child)
    source_paths(archive)
    mask = os.umask(0)
    os.umask(mask)
    seed_matches = set(re.findall(rb'/nix/store/[a-z0-9]{32}-codetracer-trace-format-nim-nimble-seed', expected_rc))
    if len(seed_matches) != 1:
        raise RuntimeError('Actual locked Nimble seed constructor is absent or ambiguous')
    seed = Path(os.fsdecode(seed_matches.pop()))
    seed_before = namespace(seed)
    module_before = image(module)
    plugin = direnv_profile / 'share/trace-cross-reader/flake-overrides.bash'
    plugin_body = plugin.read_bytes()
    # The body the peer's tracked .envrc pins for its `source_url` fallback
    # (`"sha256-<base64>"`), read from that file rather than restated here: a
    # restated digest goes stale the next time the peer moves its pin.
    envrc_pins = re.findall(r'"sha256-([A-Za-z0-9+/]{43}=)"', envrc.read_text())
    if len(envrc_pins) != 1:
        raise RuntimeError('Peer envrc plugin pin absent or ambiguous')
    if hashlib.sha256(plugin_body).hexdigest() != base64.b64decode(envrc_pins[0]).hex():
        raise RuntimeError('Plugin differs from the actual envrc pinned body')
    root_source = tracked(root)
    root_config = metadata(root, 'config', '--null', '--show-origin', '--list')
    environment_before = dict(os.environ)
    held = (peer, source, config, images, peer_namespace, root_source, root_config, environment_before)
    proof['schema'] = 'codetracer-trace-format.unix-runtime-authority.v1'
    proof['constructorSourceSha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    proof['flakeLockSha256'] = hashlib.sha256((peer / 'flake.lock').read_bytes()).hexdigest()
    proof['selectedDirenvImage'] = image(direnv_profile / 'bin/direnv')
    proof['selectedNixDirenvModuleImage'] = image(module)
    proof['peerRevision'] = revision
    proof['envrcSha256'] = hashlib.sha256(envrc.read_bytes()).hexdigest()
    common_before = Path(os.fsdecode(metadata(peer, 'rev-parse', '--path-format=absolute', '--git-common-dir').rstrip(b'\n')))
    if common_before != peer / '.git':
        raise RuntimeError('Unsupported primary native hook common directory')
    for path in (common_before, common_before / 'hooks'):
        identity = hook_directory_identity(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        if hook_directory_identity(path, descriptor) != identity:
            os.close(descriptor)
            raise RuntimeError('Native hook held directory differs')
        hook_directory_bindings.append((path, descriptor, identity))
    # The hook slots before activation, so that what the installer writes can
    # be held to exactly what it is documented to write.
    hooks_dir = common_before / 'hooks'
    hooks_before = {row[0]: row for row in namespace(hooks_dir)}
    proof['initialHookAuthorityVerified'] = True
    # Authorize only this already-authenticated tracked file, never a global allow.
    command = [str(direnv_profile / 'bin/direnv'), 'allow', str(envrc)]
    run_child(command, peer, 'authorize-exact-tracked-envrc')
    activation_environment = dict(os.environ)
    # This newly cloned peer has no prior activation. Do not let the owning
    # shell's Direnv delta restore its unrelated original PATH in this child.
    for inherited_role in ['DIRENV_DIFF', 'DIRENV_DIR', 'DIRENV_FILE', 'DIRENV_WATCHES']:
        activation_environment.pop(inherited_role, None)
    activation_environment['PATH'] = str(nix.parent) + os.pathsep + activation_environment.get('PATH', '')
    activated_nix = shutil.which('nix', path=activation_environment['PATH'])
    if not activated_nix or Path(activated_nix).resolve(strict=True) != nix.resolve(strict=True) or image(Path(activated_nix)) != image(nix):
        raise RuntimeError('Declared activation Nix role differs from selected constructor')
    exported, diagnostics = run_child([str(direnv_profile / 'bin/direnv'), 'export', 'json'], peer, 'activate-exact-locked-peer', activation_environment)
    # Successful export is a delta. An already active environment can return {}.
    # Neither absence of PATH in that delta nor frontend zero proves a fresh shell.
    delta = json.loads(exported)
    activated, selected = helper_module.apply_activation(delta, diagnostics, activation_environment, expected_roles, image)
    if activated.get('NIMBLE_DIR') != str(peer / '.nimble'):
        raise RuntimeError('Declared Nimble source cache selector differs from owned seed output')
    # The committed config is unchanged source (the ordinary namespace guards
    # hold it byte for byte); the installer wrote one hook per stage the config
    # names in `default_install_hook_types` (pre-commit's default: pre-commit),
    # into the stage's slot, or beside a reprobuild dispatcher holding it as
    # `<stage>.repro-local`. Nothing else in the hook directory changed.
    config_text = (peer / HOOK_CONFIG).read_text()
    stages = []
    in_block = False
    for line in config_text.splitlines():
        if line.startswith('default_install_hook_types:'):
            rest = line.split(':', 1)[1].strip()
            if rest.startswith('['):
                stages = [v.strip().strip('\'"') for v in rest.strip('[]').split(',') if v.strip()]
                break
            in_block = True
            continue
        if in_block:
            if re.match(r'^\s*-', line):
                stages.append(line.split('-', 1)[1].split('#', 1)[0].strip().strip('\'"'))
                continue
            if line and not line[0].isspace() and not line.startswith('#'):
                break
    stages = [stage for stage in stages if stage] or ['pre-commit']
    for value in [str(hook_pre_commit), str(hooks_dir), *stages]:
        # printf %q leaves these characters unquoted, so the expected body is
        # the literal below; anything else is refused rather than re-quoted.
        if not re.fullmatch(r'[A-Za-z0-9/._+-]+', value):
            raise RuntimeError('Native hook installer argument needs shell quoting')
    expected_hooks = {}
    for stage in stages:
        slot = stage
        before_row = hooks_before.get(stage)
        if before_row is not None and before_row[5] == 'file' and b'reprobuild hook dispatcher' in (hooks_dir / stage).read_bytes():
            slot = stage + '.repro-local'
        body = ('#!/usr/bin/env bash\n' + HOOK_MARKER + '\n' +
                'exec ' + str(hook_pre_commit) + ' hook-impl --config ' + HOOK_CONFIG +
                ' --hook-type ' + stage + ' --hook-dir ' + str(hooks_dir) + ' -- "$@"\n').encode()
        expected_hooks[slot] = hashlib.sha256(body).hexdigest()
    common = Path(os.fsdecode(metadata(peer, 'rev-parse', '--path-format=absolute', '--git-common-dir').rstrip(b'\n')))
    if common != peer / '.git' or common.is_symlink() or (common / 'hooks').is_symlink():
        raise RuntimeError('Unsupported local hook output ownership')
    hooks_after = {row[0]: row for row in namespace(hooks_dir)}
    for name, row in hooks_after.items():
        if name in expected_hooks:
            if row[5] != 'file' or row[6] != expected_hooks[name] or row[4] != os.getuid() or not row[3] & 0o100:
                raise RuntimeError('Actual native hook differs from its installer')
        elif hooks_before.get(name) != row:
            raise RuntimeError('Native hook installer changed another hook member')
    if set(hooks_before) - set(hooks_after) or set(expected_hooks) - set(hooks_after):
        raise RuntimeError('Native hook installer output incomplete or removed a member')
    hook_transition = {'path': str(hooks_dir), 'namespace': namespace(hooks_dir)}
    if not declared_peer_config_continuity(peer, config):
        raise RuntimeError('Native hook constructor changed undeclared Git configuration')
    proof['declaredHookInstallation'] = {'path': hook_transition['path'],
            'installer': installer_image, 'stages': stages, 'hooks': expected_hooks,
            'scopeLimit': 'Git configuration unchanged; all other hook members exact'}
    if activated.get('DIRENV_DIR') != '-' + str(peer):
        raise RuntimeError('Actual locked peer loaded-RC association differs')
    proof['loadedRcDirectory'] = activated['DIRENV_DIR']
    proof['activatedSelectedImages'] = selected
    generated_verified = verify_generated_outputs(peer, expected_rc, expected_profile, expected_sources, seed, seed_before, module, plugin_body, selected, activated, run_child, namespace, receipts, mask)
    # The first census refuses any preexisting non-seed metadata slot.
    metadata_bytes, _ = run_child([str(nix), 'eval', '--raw', '--offline',
        '--option', 'allow-import-from-derivation', 'false', '--no-write-lock-file',
        '.#ci-nimble-runtime-metadata.outPath'], peer, 'actual-nimble-metadata-constructor')
    metadata_root = Path(metadata_bytes.decode().strip())
    if not re.fullmatch(r'/nix/store/[a-z0-9]{32}-[^/]+', str(metadata_root)):
        raise RuntimeError('Declared metadata writer store output schema differs')
    # Realize the writer before authenticating it. Nothing else in this
    # constructor builds it (the peer's dev shell does not include it), so on a
    # store that has not built it before the registered-closure census below
    # had nothing to read. The build may substitute or compile; either way the
    # output must be the path just evaluated, and the census and content
    # verification below still decide whether it is admitted.
    built_bytes, _ = run_child([str(nix), 'build', '--no-link', '--print-out-paths',
        '--option', 'allow-import-from-derivation', 'false', '--no-write-lock-file',
        '.#ci-nimble-runtime-metadata'], peer, 'actual-metadata-writer-realization')
    if built_bytes.decode().strip() != str(metadata_root):
        raise RuntimeError('Realized metadata writer differs from the evaluated output')
    drv_bytes, _ = run_child([str(nix), 'eval', '--raw', '--offline',
        '--option', 'allow-import-from-derivation', 'false', '--no-write-lock-file',
        '.#ci-nimble-runtime-metadata.drvPath'], peer, 'actual-metadata-writer-derivation')
    drv = drv_bytes.decode().strip()
    drv_json, _ = run_child([str(nix), 'derivation', 'show', drv], peer,
        'actual-metadata-writer-derivation-attributes')
    descriptions = json.loads(drv_json)
    if not isinstance(descriptions, dict) or set(descriptions) != {drv}:
        raise RuntimeError('Actual writer derivation inventory differs')
    attributes = descriptions[drv]
    if not isinstance(attributes.get('outputs'), dict) or set(attributes['outputs']) != {'out'} or attributes['outputs']['out'].get('path') != str(metadata_root):
        raise RuntimeError('Actual selected writer output role differs')
    source_bytes, _ = run_child([str(nix), 'eval', '--raw', '--offline',
        '--option', 'allow-import-from-derivation', 'false', '--no-write-lock-file',
        '.#ci-nimble-source.outPath'], peer, 'actual-metadata-selected-nimble-source')
    source_root = source_bytes.decode().strip()
    writer_source = peer / 'nix/construct-runtime-metadata.nim'
    source_matches = re.findall(r'/nix/store/[a-z0-9]{32}-construct-runtime-metadata\.nim', attributes['env']['buildPhase'])
    if len(source_matches) != 1 or Path(source_matches[0]).read_bytes() != writer_source.read_bytes() or attributes['env']['src'] != source_root or attributes['env']['out'] != str(metadata_root):
        raise RuntimeError('Writer source/output differs from actual held constructor')
    images.append(image(writer_source)); images.append(image(Path(source_matches[0])))
    closure_json, _ = run_child([str(nix), 'path-info', '--json', '--recursive', str(metadata_root)], peer,
        'actual-metadata-writer-registered-closure')
    closure = json.loads(closure_json)
    if not isinstance(closure, dict) or str(metadata_root) not in closure or any(not isinstance(v, dict) or not isinstance(v.get('narHash'), str) or not isinstance(v.get('references'), list) or not set(v['references']).issubset(closure) for v in closure.values()):
        raise RuntimeError('Actual writer recursive registered closure incomplete')
    run_child([str(nix), 'store', 'verify', '--recursive', '--no-trust', str(metadata_root)],
        peer, 'actual-nimble-metadata-runtime-content')
    metadata_runtime_trees = {path: namespace(Path(path)) for path in closure}
    proof['actualMetadataRuntimeRegisteredClosure'] = closure
    writer = metadata_root / 'bin/construct-runtime-metadata'
    writer_before = image(writer)
    images.append(writer_before)
    writer_result, _ = run_child([str(writer), str(peer / '.nimble')], peer,
        'genuine-nimble-two-metadata-writers', activated)
    producer_result = json.loads(writer_result)
    if set(producer_result) != {'installedPackages', 'releaseCount'} or not isinstance(producer_result['installedPackages'], list) or not isinstance(producer_result['releaseCount'], int) or producer_result['releaseCount'] <= 0:
        raise RuntimeError('Genuine metadata writer result differs')
    expected_installed = []
    for directory in sorted((seed / 'pkgs2').iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            raise RuntimeError('Unsupported actual seed package member')
        match = re.fullmatch(r'([A-Za-z0-9_]+)-([0-9]+(?:\.[0-9]+){1,3})-([a-f0-9]{40})', directory.name)
        if match is None or not (directory / (match.group(1) + '.nimble')).is_file():
            raise RuntimeError('Unsupported selected seed package constructor')
        expected_installed.append({'name': match.group(1), 'version': match.group(2), 'checksum': match.group(3)})
    installed = producer_result['installedPackages']
    if any(not isinstance(v, dict) or set(v) != {'name', 'version', 'checksum'} or any(not isinstance(item, str) for item in v.values()) for v in installed):
        raise RuntimeError('Actual installed package inventory schema differs')
    if sorted(installed, key=lambda v: (v['name'], v['version'], v['checksum'])) != sorted(expected_installed, key=lambda v: (v['name'], v['version'], v['checksum'])):
        raise RuntimeError('Actual metadata writer inventory differs from independently held complete seed')
    proof['expectedSeedInstalledPackages'] = expected_installed
    # The original reader task registers dependency reverse edges and produces
    # its named worktree-local cache before the final runtime authority is sealed.
    reader_before = namespace(peer)
    expected_data_bytes, _ = run_child([str(writer), str(peer / '.nimble'),
        '--expected-reader-data', str(peer)], peer, 'actual-reader-dependency-constructor', activated)
    expected_reader_data = json.loads(expected_data_bytes)
    if namespace(peer) != reader_before:
        raise RuntimeError('Read-only reader dependency constructor changed the peer')
    reader = peer / 'ct-print'
    cache_root = '.nimcache/src/codetracer_ct_print_r'
    if reader.exists() or reader.is_symlink() or (peer / '.nimcache').exists() or (peer / '.nimcache').is_symlink():
        raise RuntimeError('Preexisting reader/compiler outputs refused')
    run_child([proof['expectedSelectedImages']['nimble']['lexical'], 'buildCtPrint'],
        peer, 'actual-source-declared-reader-build', activated)
    reader_after = namespace(peer)
    def reader_member(name):
        return name == 'ct-print' or name in {'.nimcache', '.nimcache/src', cache_root} or name.startswith(cache_root + '/')
    prior = {row[0]: row for row in reader_before}
    current = {row[0]: row for row in reader_after}
    metadata_name = '.nimble/nimbledata2.json'
    if any(current.get(name) != row for name, row in prior.items() if name != metadata_name) or any(not reader_member(name) for name in current.keys() - prior.keys()):
        raise RuntimeError('Declared reader changed another execution member')
    if current[metadata_name][:-1] != prior[metadata_name][:-1]:
        raise RuntimeError('Declared reader changed metadata physical ownership')
    # ReverseDependency arrays have set semantics in the actual addRevDep API.
    def canonical_reverse_data(value):
        if not isinstance(value, dict) or set(value) != {'version', 'reverseDeps'} or value['version'] != 1 or not isinstance(value['reverseDeps'], dict):
            raise RuntimeError('Unsupported reader reverse-dependency schema')
        return {name: {version: {checksum: sorted(json.dumps(row, sort_keys=True) for row in rows)
                    for checksum, rows in checksums.items()} for version, checksums in versions.items()}
                    for name, versions in value['reverseDeps'].items()}
    actual_reader_data = json.loads((peer / metadata_name).read_bytes())
    if canonical_reverse_data(actual_reader_data) != canonical_reverse_data(expected_reader_data):
        raise RuntimeError('Reader dependency metadata differs from actual selected Nimble constructor')
    if not {'ct-print', '.nimcache', '.nimcache/src', cache_root}.issubset(current):
        raise RuntimeError('Declared reader cache constructor output incomplete')
    reader_rows = [row for row in reader_after if reader_member(row[0])]
    if not any(row[0] == 'ct-print' and row[5] == 'file' and row[3] & 0o111 for row in reader_rows):
        raise RuntimeError('Genuine reader output absent or nonexecutable')
    for row in reader_rows:
        path = peer / row[0]; physical = path.lstat()
        if row[4] != os.getuid() or row[5] not in {'dir', 'file'} or (row[5] == 'file' and physical.st_nlink != 1):
            raise RuntimeError('Foreign or nonregular reader/compiler output')
    proof['declaredReaderConstructor'] = {'image': image(reader), 'outputs': reader_rows,
        'expectedReverseDependencies': expected_reader_data}
    images.append(image(reader))
    source_namespace = [row for row in reader_after if row[0].split('/')[0] not in generated_roots]
    proof['ordinarySourceNamespace'] = source_namespace
    metadata_slots = {}
    for name in ['nimbledata2.json', 'official-nim-releases.json']:
        path = peer / '.nimble' / name
        value = path.lstat()
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_uid != os.getuid():
            raise RuntimeError('Genuine metadata constructor output ownership differs')
        body = path.read_bytes()
        metadata_slots[name] = {'sha256': hashlib.sha256(body).hexdigest(), 'value': json.loads(body)}
    data = metadata_slots['nimbledata2.json']['value']
    versions = metadata_slots['official-nim-releases.json']['value']
    if not isinstance(data, dict) or set(data) != {'version', 'reverseDeps'} or data['version'] != 1 or not isinstance(data['reverseDeps'], dict):
        raise RuntimeError('Actual selected Nimble data schema differs')
    if not isinstance(versions, list) or len(versions) != producer_result['releaseCount'] or any(not isinstance(v, dict) or set(v) != {'version'} or not isinstance(v['version'], str) for v in versions):
        raise RuntimeError('Actual selected release data schema differs')
    proof['actualNimbleMetadataConstructor'] = {'image': writer_before, 'result': producer_result, 'slots': metadata_slots}
    if image(writer) != writer_before:
        raise RuntimeError('Actual metadata writer changed during execution')
    generated_verified = verify_generated_outputs(peer, expected_rc, expected_profile, expected_sources, seed, seed_before, module, plugin_body, selected, activated, run_child, namespace, receipts, mask, metadata_slots)
    generated_after = [row for row in namespace(peer) if row[0].split('/')[0] in generated_roots]
    proof['generatedOutputAfter'] = generated_after
    proof['mandatoryNimbleMetadataSlots'] = metadata_slots
    proof['success'] = True
except BaseException as error:
    proof['failureType'] = type(error).__name__
    # Our RuntimeError predicates have constant messages. Other exceptions may
    # carry configuration/export payloads and are represented only by type.
    if isinstance(error, RuntimeError):
        proof['failure'] = str(error)
        print(str(error), file=sys.stderr)
finally:
    proof['authorityGuards'] = {}
    if held:
        checks = {
            'peerTracked': lambda: tracked(held[0]) == held[1],
            'peerConfigMemory': lambda: declared_peer_config_continuity(held[0], held[2]),
            'selectedImages': lambda: [image(Path(p['lexical'])) for p in held[3]] == held[3],
            'peerOrdinaryNamespace': lambda: [row for row in namespace(peer) if row[0].split('/')[0] not in generated_roots] == source_namespace,
            'rootTracked': lambda: tracked(root) == held[5],
            'rootConfigMemory': lambda: metadata(root, 'config', '--null', '--show-origin', '--list') == held[6],
            'constructorEnvironmentMemory': lambda: dict(os.environ) == held[7],
        }
        for name, check in checks.items():
            try:
                proof['authorityGuards'][name] = bool(check())
            except BaseException as error:
                proof['authorityGuards'][name] = False
                proof.setdefault('guardErrors', {})[name] = type(error).__name__
    try:
        proof['finalGuards'] = bool(all(namespace(Path(p)) == rows for p, rows in metadata_runtime_trees.items()) and held and tracked(held[0]) == held[1] and declared_peer_config_continuity(held[0], held[2]) and [image(Path(p['lexical'])) for p in held[3]] == held[3] and [row for row in namespace(peer) if row[0].split('/')[0] not in generated_roots] == source_namespace and generated_verified and [row for row in namespace(peer) if row[0].split('/')[0] in generated_roots] == generated_after and namespace(seed) == seed_before and image(module) == module_before and image(hook_pre_commit) == hook_pre_commit_before and tracked(root) == held[5] and metadata(root, 'config', '--null', '--show-origin', '--list') == held[6] and dict(os.environ) == held[7] and all(image(Path(v['lexical'])) == v for v in proof.get('activatedSelectedImages', {}).values()) and all(image(Path(v['lexical'])) == v for v in proof.get('expectedSelectedImages', {}).values()))
    except Exception:
        proof['finalGuards'] = False
    proof['success'] = bool(proof['success'] and proof['finalGuards'])
    proof['finished'] = True
    save()
    for _, descriptor, _ in hook_directory_bindings:
        os.close(descriptor)
    if not proof['finalGuards'] and 'failureType' not in proof:
        print('Runtime prerequisite final authority changed', file=sys.stderr)
if not proof['success']:
    raise SystemExit(1)
