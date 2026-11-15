{.push raises: [].}

## Fixture generator of the declared-root cross-read: one Nim-written trace
## in five container geometries, and what the Nim reader reads from each.
##
## A container's block size and the size of its root directory are fields of
## its header (`codetracer-trace-format-spec/ctfs-container.md` §1, "Container
## Header"): `BlockSize` is 1024, 2048 or 4096, `MaxRootEntries` is fixed when
## the container is created, and a declared `0` is the auto-filled root, 170
## slots at 4096 bytes ("Block 0 Layout"). A reader takes both from the
## container it opens. A reader that scans a fixed 31 slots reads a trace whose
## members lie past slot 30 as an empty container, and one that resolves
## members with a fixed 4096-byte block refuses a 1024- or 2048-byte container.
##
## The trace is written by the Nim multi-stream writer (4096 bytes, 31 slots)
## to `<out.ct>`, the base, and rebuilt member for member, every member's bytes
## unchanged, beside it:
##
## * `same.ct`    -- 4096 bytes, 31 slots: the control, the writer's geometry.
## * `shift31.ct` -- 4096 bytes, `MaxRootEntries = 0`, 31 one-byte filler
##   members in slots 0 to 30 and the trace after them.
## * `root64.ct`  -- 4096 bytes, 64 slots, the same fillers first.
## * `bs1024.ct`, `bs2048.ct` -- 1024- and 2048-byte blocks, 31 slots.
##
## The sidecar `<out.ct>.declared-root.txt` has one line per container, the
## base first:
##
##     <name> <BlockSize> <slots scanned> <members> <paths> <functions> <steps>
##
## the geometry and member count read from the header and root directory, and
## the three counts the Nim reader reports. The reader is asked twice, through
## `openNewTrace` (by path) and `openNewTraceFromBytes`, and the generator
## fails if the two disagree. The Rust half
## (`tests/nim_declared_root_crossread.rs`) reads the same containers with the
## Rust readers and compares.
##
## Usage: `gen_declared_root_crossread_fixture <out.ct>`.

import std/[os, strutils]
import results
import codetracer_ctfs
import codetracer_trace_writer/multi_stream_writer
import codetracer_trace_writer/new_trace_reader

proc fail(msg: string) {.noreturn.} =
  try:
    stderr.writeLine("gen_declared_root_crossread_fixture: " & msg)
  except IOError, ValueError:
    discard
  quit(1)

proc recording(): seq[byte] =
  ## 3,000 steps over three files, a call every 7 steps and a value on every
  ## step: `steps.dat` and `values.dat` span several blocks at every block
  ## size, so their members are resolved through mapping blocks.
  var wRes = initMultiStreamWriter("", "declared_root_crossread",
    recordingId = "01949fcc-7d92-7e9c-aaaa-dddddddddddd")
  if wRes.isErr: fail("init writer: " & wRes.error)
  var w = wRes.get()
  for p in ["/src/a.nim", "/src/b.nim", "/src/c.nim"]:
    if w.registerPath(p).isErr: fail("registerPath " & p)
  let work = w.registerFunction("work")
  if work.isErr: fail("registerFunction: " & work.error)
  if w.registerFunction("rest").isErr: fail("registerFunction rest")
  for i in 0 ..< 3000:
    let vals = @[VariableValue(varnameId: uint64(i mod 5),
      data: @[byte(i and 0xff), byte(i shr 8)])]
    let s = w.registerStep(uint64(i mod 3), uint64(1 + (i * 7) mod 300), vals)
    if s.isErr: fail("registerStep: " & s.error)
    if i mod 7 == 0:
      let c = w.registerCall(work.get(), @[])
      if c.isErr: fail("registerCall: " & c.error)
    if i mod 7 == 3:
      let r = w.registerReturn(@[byte(9)])
      if r.isErr: fail("registerReturn: " & r.error)
  let closed = w.close()
  if closed.isErr: fail("close: " & closed.error)
  result = w.toBytes()
  discard w.closeCtfs()

proc members(data: seq[byte]): seq[(string, seq[byte])] =
  ## Every member of a full-profile container, in slot order, read with the
  ## geometry its header declares.
  let bs = readU32LE(data, 8)
  let slots = headerRootEntryCount(data)
  let base = containerHeaderSize(data)
  for i in 0 ..< int(slots):
    let nameWord = readU64LE(data, base + i * FileEntrySize + 16)
    if nameWord == 0: continue
    let name = base40Decode(nameWord)
    let r = readInternalFile(data, name, bs, slots)
    if r.isErr: fail(name & ": " & r.error)
    result.add((name, r.get()))

proc readFileBytes(path: string): seq[byte] =
  try:
    let s = readFile(path)
    result = newSeq[byte](s.len)
    for i, c in s: result[i] = byte(c)
  except IOError:
    fail("cannot read " & path)

proc writeFileBytes(path: string, bytes: openArray[byte]) =
  var s = newString(bytes.len)
  for i, b in bytes: s[i] = char(b)
  try:
    writeFile(path, s)
  except IOError:
    fail("cannot write " & path)

proc rebuild(src: seq[(string, seq[byte])], path: string, blockSize: uint32,
             maxRootEntries: uint32, fillers: int) =
  ## `src` written into a new container of the given geometry at `path`,
  ## after `fillers` one-byte members.
  var c = createCtfs(blockSize = blockSize, maxRootEntries = maxRootEntries)
  for i in 0 ..< fillers:
    let f = c.addFile("f" & align($i, 2, '0') & ".dat")
    if f.isErr: fail("addFile filler: " & f.error)
    var fh = f.get()
    if c.writeToFile(fh, [byte(i)]).isErr: fail("write filler")
  for (name, bytes) in src:
    let f = c.addFile(name)
    if f.isErr: fail("addFile " & name & ": " & f.error)
    var fh = f.get()
    if bytes.len > 0 and c.writeToFile(fh, bytes).isErr:
      fail("write " & name)
  if writeCtfsToFile(c, path).isErr: fail("writeCtfsToFile " & path)

proc nimCounts(r: var NewTraceReader, what: string): string =
  let steps = r.stepCount()
  if steps.isErr: fail(what & ": stepCount: " & steps.error)
  $r.pathCount() & " " & $r.functionCount() & " " & $steps.get()

proc row(name, path: string): string =
  ## The sidecar line of the container at `path`.
  let bytes = readFileBytes(path)
  let slots = headerRootEntryCount(bytes)
  var populated = 0
  for i in 0 ..< int(slots):
    if readU64LE(bytes, containerHeaderSize(bytes) + i * FileEntrySize + 16) != 0:
      inc populated
  var byPath = openNewTrace(path)
  if byPath.isErr: fail(name & ": openNewTrace: " & byPath.error)
  let a = nimCounts(byPath.get(), name & " openNewTrace")
  var fromBytes = openNewTraceFromBytes(bytes)
  if fromBytes.isErr: fail(name & ": openNewTraceFromBytes: " & fromBytes.error)
  let b = nimCounts(fromBytes.get(), name & " openNewTraceFromBytes")
  if a != b:
    fail(name & ": openNewTrace read " & a & ", openNewTraceFromBytes " & b)
  name & " " & $readU32LE(bytes, 8) & " " & $slots & " " & $populated & " " & a

proc main() =
  let args = commandLineParams()
  if args.len < 1:
    fail("usage: gen_declared_root_crossread_fixture <out.ct>")
  let outPath = args[0]
  let dir = outPath.parentDir()
  let base = recording()
  if readU32LE(base, 8) != 4096'u32 or readU32LE(base, 12) != 31'u32:
    fail("the writer's geometry is not 4096 bytes and 31 slots")
  writeFileBytes(outPath, base)
  let src = members(base)
  var sidecar = row("base", outPath) & "\n"
  for (name, bs, declared, fillers) in [
      ("same", 4096'u32, 31'u32, 0),
      ("shift31", 4096'u32, 0'u32, 31),
      ("root64", 4096'u32, 64'u32, 31),
      ("bs1024", 1024'u32, 31'u32, 0),
      ("bs2048", 2048'u32, 31'u32, 0)]:
    let path = dir / (name & ".ct")
    rebuild(src, path, bs, declared, fillers)
    sidecar.add(row(name, path) & "\n")
  writeFileBytes(outPath & ".declared-root.txt", sidecar.toOpenArrayByte(0,
    sidecar.len - 1))
  try:
    stdout.write(sidecar)
  except IOError:
    discard

main()
