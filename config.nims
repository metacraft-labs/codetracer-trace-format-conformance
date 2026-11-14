## Compiler configuration of every Nim test in `nim/`.
##
## The tests are compiled against the Nim implementation checked out beside
## this repository, `../codetracer-trace-format-nim`, exactly as that
## repository's own tests are compiled against its `src`. Its Nim
## dependencies (`results`, `stew`) come from the `NIMBLE_DIR` the dev shell
## seeds (see flake.nix).
import std/os

let nimLibrary = thisDir() / ".." / "codetracer-trace-format-nim" / "src"
doAssert dirExists(nimLibrary),
  "the Nim implementation is not checked out at " & nimLibrary &
  " (this repository's tests need both implementations beside it)"
switch("path", nimLibrary)
switch("path", thisDir() / "nim")
