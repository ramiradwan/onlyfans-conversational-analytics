<#
    Verify that released artifacts carry a valid, timestamped Authenticode
    signature.

    A signature without a countersignature timestamp verifies while the signing
    certificate is valid and stops verifying once it expires. Short-lived
    signing certificates make that the normal outcome rather than an edge case,
    so an untimestamped signature is rejected here.

    Exit codes are distinct so that a caller can tell the three rejections
    apart:

        0  every resolved file carries a valid, timestamped signature
        2  no file was resolved, or a named path is neither file nor directory
        3  at least one resolved file has no valid Authenticode signature
        4  at least one signature is valid but carries no timestamp
#>
[CmdletBinding()]
param(
    # Files to verify, or directories whose signable files are verified. The
    # default is deliberately empty so that a caller that resolves nothing is
    # rejected instead of reported as verified.
    [AllowEmptyCollection()]
    [string[]] $Path = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Extensions that carry an embedded Authenticode signature. Other published
# artifacts, such as the Agent archive, are never resolved from a directory.
$SignableExtensions = @(".exe", ".dll", ".sys", ".ocx", ".msi", ".cab", ".cat")

$ExitVerified = 0
$ExitInputRejected = 2
$ExitSignatureRejected = 3
$ExitTimestampRejected = 4

function Resolve-SignableFile {
    param([Parameter(Mandatory)] [AllowEmptyCollection()] [string[]] $Path)

    $resolved = [System.Collections.Generic.List[string]]::new()
    foreach ($candidate in $Path) {
        if ([string]::IsNullOrWhiteSpace($candidate)) {
            continue
        }
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $resolved.Add([IO.Path]::GetFullPath($candidate))
            continue
        }
        if (Test-Path -LiteralPath $candidate -PathType Container) {
            $inside = @(
                Get-ChildItem -LiteralPath $candidate -Recurse -File |
                    Where-Object { $SignableExtensions -contains $_.Extension.ToLowerInvariant() } |
                    Sort-Object FullName
            )
            if ($inside.Count -eq 0) {
                Write-Host "verify-signatures: input rejected, no signable file under $candidate"
                exit $ExitInputRejected
            }
            foreach ($file in $inside) {
                $resolved.Add($file.FullName)
            }
            continue
        }
        Write-Host "verify-signatures: input rejected, neither a file nor a directory: $candidate"
        exit $ExitInputRejected
    }
    return $resolved
}

$files = @(Resolve-SignableFile -Path $Path)
if ($files.Count -eq 0) {
    Write-Host "verify-signatures: input rejected, no file was named"
    exit $ExitInputRejected
}

$rejected = [System.Collections.Generic.List[string]]::new()
$untimestamped = [System.Collections.Generic.List[string]]::new()
foreach ($file in $files) {
    $signature = Get-AuthenticodeSignature -LiteralPath $file
    $timestamped = $null -ne $signature.TimeStamperCertificate
    Write-Host ("verify-signatures: {0} timestamped={1} {2}" -f $signature.Status, $timestamped, $file)
    if ($signature.Status -ne "Valid") {
        $rejected.Add($file)
        continue
    }
    if (-not $timestamped) {
        $untimestamped.Add($file)
    }
}

# An invalid signature is reported ahead of a missing timestamp: a file that
# fails both is a signature rejection, not a timestamp rejection.
if ($rejected.Count -gt 0) {
    Write-Host "verify-signatures: signature check rejected $($rejected.Count) of $($files.Count) file(s)"
    exit $ExitSignatureRejected
}

if ($untimestamped.Count -gt 0) {
    Write-Host "verify-signatures: timestamp check rejected $($untimestamped.Count) of $($files.Count) file(s)"
    exit $ExitTimestampRejected
}

Write-Host "verify-signatures: signature check accepted $($files.Count) file(s), all timestamped"
exit $ExitVerified
