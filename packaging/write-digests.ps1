<#
    Write `<sha256> *<relative/path>` records into sha256sums.txt beside the
    files they describe. A digest file only ever covers files that sit beside
    it, so a listed path that is absent fails instead of being recorded.
#>
[CmdletBinding(DefaultParameterSetName = "FilesBeside")]
param(
    # Directory that receives sha256sums.txt.
    [Parameter(Mandatory)]
    [string] $Directory,

    # Paths to record, relative to -Directory. When omitted, every file
    # beneath -Directory other than the digest file itself is recorded.
    [Parameter(Mandatory, ParameterSetName = "Explicit")]
    [AllowEmptyCollection()]
    [string[]] $RelativePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$DigestFileName = "sha256sums.txt"

if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
    throw "Digest directory does not exist: $Directory"
}
$Directory = [IO.Path]::GetFullPath($Directory)

if ($PSCmdlet.ParameterSetName -eq "FilesBeside") {
    $RelativePath = @(
        Get-ChildItem -LiteralPath $Directory -Recurse -File |
            Sort-Object FullName |
            ForEach-Object { ($_.FullName.Substring($Directory.Length).TrimStart('\', '/')) -replace '\\', '/' } |
            Where-Object { $_ -ne $DigestFileName }
    )
}

$lines = foreach ($relative in @($RelativePath | Sort-Object)) {
    $path = Join-Path $Directory $relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "A digest file may only record files beside it; this one is absent: $path"
    }
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = [BitConverter]::ToString($hasher.ComputeHash([IO.File]::ReadAllBytes($path))).Replace("-", "").ToLowerInvariant()
    } finally {
        $hasher.Dispose()
    }
    "{0} *{1}" -f $digest, ($relative -replace '\\', '/')
}
Set-Content -LiteralPath (Join-Path $Directory $DigestFileName) -Value $lines -Encoding ascii
