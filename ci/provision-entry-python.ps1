param(
  [Parameter(Mandatory = $true)]
  [ValidateSet("arm64", "x64")]
  [string]$Architecture,
  [Parameter(Mandatory = $true)]
  [string]$Destination
)

# The native entry gate's declared Python (ci/native-entry-provider.py refuses
# any version but 3.13.16), taken from python.org's own NuGet packages at
# exact SHA-256 pins and extracted under the runner's temp directory.
#
# Not actions/setup-python: that runs the CPython installer into the runner's
# shared tool cache whenever the version is not cached already, and on the
# persistent Windows x64 runner that install failed in every run
# ("Error happened during Python installation", codetracer-trace-format
# windows-tests jobs 130480936473, 130640313717, 131107935768, 131263150439
# and 131632856620). The NuGet package is the same python.org build as a
# relocatable directory: nothing is installed or registered, and nothing
# outside $Destination is written.
#
# Packages: https://www.nuget.org/packages/python/3.13.16 (x64) and
# https://www.nuget.org/packages/pythonarm64/3.13.16 (ARM64). The SHA-256
# pins below were computed from the downloaded packages, whose SHA-512 matched
# the packageHash NuGet's catalog publishes for them.

$ErrorActionPreference = "Stop"
$version = "3.13.16"
$pins = @{
  arm64 = @{
    package = "pythonarm64"
    sha256 = "bfd77aa5b0edff4a7936a3220e8b79e7c78f864ce13e8fa2c835d9946e11694a"
    machine = 0xaa64
    operatingSystemArchitecture = "Arm64"
    platformMachine = "ARM64"
  }
  x64 = @{
    package = "python"
    sha256 = "95fad176338f1d6a799e45946488063f4aa61764e6cff2f132ddeffbf7a04c34"
    machine = 0x8664
    operatingSystemArchitecture = "X64"
    platformMachine = "AMD64"
  }
}
$pin = $pins[$Architecture]
if (-not $IsWindows -or [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() -ne $pin.operatingSystemArchitecture) {
  throw "Pinned Python architecture differs from the native Windows host."
}
$parent = [System.IO.Path]::GetFullPath($env:RUNNER_TEMP)
$root = [System.IO.Path]::GetFullPath($Destination)
if ([System.IO.Path]::GetDirectoryName($root) -ne $parent) {
  throw "Python extraction must be a direct child of the declared runner temp directory."
}
$parentItem = Get-Item -LiteralPath $parent -Force
if ($parentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
  throw "Python extraction parent is a reparse point."
}
if ($null -ne (Get-Item -LiteralPath $root -Force -ErrorAction SilentlyContinue)) {
  throw "Occupied Python extraction destination."
}
New-Item -ItemType Directory -Path $root -ErrorAction Stop | Out-Null
$rootItem = Get-Item -LiteralPath $root -Force
$rootCreation = $rootItem.CreationTimeUtc
$archive = Join-Path $root "package.zip"
$url = "https://www.nuget.org/api/v2/package/$($pin.package)/$version"
Invoke-WebRequest -Uri $url -OutFile $archive
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pin.sha256) {
  throw "Pinned Python package checksum differs."
}
# Only the package's `tools/` tree is the Python installation; the rest is
# NuGet metadata. Every member is still checked before anything is extracted.
$extracted = Join-Path $root "python"
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($archive)
$extractedPrefix = $extracted + [System.IO.Path]::DirectorySeparatorChar
$seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
$expandedBytes = 0L
try {
  $members = @()
  foreach ($entry in $zip.Entries) {
    $name = $entry.FullName.Replace("\", "/")
    if ($name.Split("/") -contains ".." -or $name -match "[:\x00]" -or $name.StartsWith("/")) {
      throw "Foreign Python package member path."
    }
    $kind = ($entry.ExternalAttributes -shr 16) -band 0xf000
    if ($kind -notin @(0, 0x8000, 0x4000)) {
      throw "Nonregular Python package member."
    }
    if (-not $name.StartsWith("tools/", [System.StringComparison]::Ordinal)) {
      continue
    }
    $target = [System.IO.Path]::GetFullPath((Join-Path $extracted $name.Substring(6)))
    if (-not $target.StartsWith($extractedPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or -not $seen.Add($target.TrimEnd([System.IO.Path]::DirectorySeparatorChar))) {
      throw "Aliased Python package member path."
    }
    $expandedBytes += $entry.Length
    if ($expandedBytes -gt 512MB) {
      throw "Python package exceeds its finite extraction allowance."
    }
    $members += , @($entry, $target, $name.EndsWith("/"))
  }
  if ($members.Count -eq 0) {
    throw "Python package has no tools tree."
  }
  foreach ($member in $members) {
    $entry, $target, $isDirectory = $member
    if ($isDirectory) {
      New-Item -ItemType Directory -Force -Path $target | Out-Null
      continue
    }
    New-Item -ItemType Directory -Force -Path ([System.IO.Path]::GetDirectoryName($target)) | Out-Null
    [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $false)
  }
} finally {
  $zip.Dispose()
}
$after = Get-Item -LiteralPath $root -Force
if ($after.CreationTimeUtc -ne $rootCreation -or $after.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
  throw "Owned Python extraction directory changed."
}
$executable = Join-Path $extracted "python.exe"
$item = Get-Item -LiteralPath $executable -Force
if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
  throw "Pinned Python executable is a reparse point."
}
$bytes = [System.IO.File]::ReadAllBytes($executable)
if ($bytes.Length -lt 64 -or $bytes[0] -ne 0x4d -or $bytes[1] -ne 0x5a) {
  throw "Pinned Python executable has no DOS header."
}
$offset = [System.BitConverter]::ToUInt32($bytes, 0x3c)
if ($offset -gt $bytes.Length - 24 -or [System.BitConverter]::ToUInt32($bytes, $offset) -ne 0x00004550 -or [System.BitConverter]::ToUInt16($bytes, $offset + 4) -ne $pin.machine) {
  throw "Pinned Python executable machine differs."
}
# -I: no user site, no PYTHON* variables, no script directory on sys.path.
$probe = @(& $executable -I -B -c "import platform, sys; print('%d.%d.%d' % sys.version_info[:3], platform.machine().upper(), sys.base_prefix)")
if ($LASTEXITCODE -ne 0 -or $probe.Count -ne 1) {
  throw "Pinned Python executable did not run."
}
$fields = $probe[0].Split(" ", 3)
if ($fields[0] -ne $version -or $fields[1] -ne $pin.platformMachine) {
  throw "Pinned Python executable version or machine differs."
}
if (-not [string]::Equals([System.IO.Path]::GetFullPath($fields[2]).TrimEnd([System.IO.Path]::DirectorySeparatorChar), $extracted, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Pinned Python executable does not run from its own extraction."
}
Add-Content -LiteralPath $env:GITHUB_OUTPUT -Value "python-path=$executable"
Write-Host "Pinned native Python $version selected from $($pin.package); package SHA256 $($pin.sha256)."
