## The shared plumbing of the cross-read tests: a Nim-written container handed
## to a Rust reader.
##
## Each cross-read is two halves. The Nim half (a driver in this directory)
## writes a production `.ct` container with the Nim writer from
## `../codetracer-trace-format-nim`, and records what the Nim reader decodes
## from it in a sidecar file. The Rust half is an integration test of this
## repository's Cargo package (`tests/<name>.rs`), which reads the same
## container with the Rust readers from `../codetracer-trace-format` and
## requires the decode to equal the sidecar. The driver runs that test through
## `cargo test`, with the container and sidecar paths in environment
## variables.
##
## A missing prerequisite (no `cargo` on `PATH`, no Rust half to run) FAILS the
## test. There is no skip arm: this repository exists to make these proofs, so
## a pass without one would be a false green.

import std/[os, osproc, strtabs, streams]
import runtime_nim_compile

proc conformanceRoot*(): string =
  ## The root of this repository: the Cargo package the Rust halves belong to
  ## and the directory the generator paths are relative to.
  currentSourcePath().parentDir().parentDir()

proc prerequisiteMissing*(reason: string) {.noreturn.} =
  ## Fail the test because it cannot make its proof.
  stderr.writeLine "FAIL: " & reason & " (a cross-read may not skip)"
  quit 1

proc runProcess*(cmd: string, args: seq[string], workdir: string,
    extraEnv: seq[(string, string)] = @[]): tuple[code: int, output: string] =
  ## Run a process with `extraEnv` added to this process's environment,
  ## capturing stdout and stderr together. A process that cannot be started
  ## reports exit code -1 and the error as its output.
  try:
    var env = newStringTable(modeStyleInsensitive)
    for k, v in envPairs():
      env[k] = v
    for (k, v) in extraEnv:
      env[k] = v
    let p = startProcess(cmd, workingDir = workdir, args = args, env = env,
      options = {poStdErrToStdOut, poUsePath})
    let outp = p.outputStream.readAll()
    let code = p.waitForExit()
    p.close()
    (code, outp)
  except OSError, IOError, Exception:
    (-1, "process error: " & getCurrentExceptionMsg())

proc runRustHalf*(rustTest: string,
    env: seq[(string, string)]): tuple[code: int, output: string] =
  ## Run the Rust half `tests/<rustTest>.rs` of this repository's package
  ## against the container and sidecar named in `env`.
  let root = conformanceRoot()
  if findExe("cargo").len == 0:
    prerequisiteMissing("no cargo on PATH to run the Rust reader")
  if not fileExists(root / "tests" / (rustTest & ".rs")):
    prerequisiteMissing("the Rust half tests/" & rustTest & ".rs is missing")
  runProcess("cargo", @["test", "--locked", "--test", rustTest, "--",
    "--nocapture"], root, env)

proc streamCrossRead*(label, generator, rustTest, sidecarSuffix,
    fixtureVar, sidecarVar, failure: string) =
  ## The three stream cross-reads differ only in their names: build and run
  ## `generator` (repository-relative), which writes `<bundle>` and
  ## `<bundle><sidecarSuffix>`, then run `rustTest` with `fixtureVar` and
  ## `sidecarVar` naming them. `failure` heads the message of a Rust-side
  ## failure.
  let root = conformanceRoot()
  let tmp = getTempDir() / (label & "_" & $getCurrentProcessId())
  try:
    createDir(tmp)
  except OSError, IOError:
    prerequisiteMissing("cannot create temp dir " & tmp)

  # 1. Build the fixture generator, with the toolchain this test was compiled
  # with rather than the ambient `nim` (`runtime_nim_compile.nim`).
  let genBin = tmp / "gen"
  let buildRes = compileWithTestToolchain(root / generator, genBin,
    tmp / "nimcache")
  doAssert buildRes.exitCode == 0,
    "fixture generator build failed:\n" & buildRes.output

  # 2. Produce the bundle and the sidecar of what the Nim reader decodes.
  let bundle = tmp / "trace.ct"
  let genRes = runProcess(genBin, @[bundle], root)
  doAssert genRes.code == 0, "fixture generation failed:\n" & genRes.output
  doAssert fileExists(bundle), "fixture bundle not written: " & bundle
  let sidecar = bundle & sidecarSuffix
  doAssert fileExists(sidecar), "fixture sidecar not written: " & sidecar

  # 3. Read the Nim bundle with the Rust reader.
  let rustRes = runRustHalf(rustTest,
    @[(fixtureVar, bundle), (sidecarVar, sidecar)])
  doAssert rustRes.code == 0, failure & ":\n" & rustRes.output

  try:
    removeDir(tmp)
  except OSError, IOError:
    discard
