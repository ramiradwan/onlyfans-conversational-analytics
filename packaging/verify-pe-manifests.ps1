<# Read embedded PE manifests with the pinned SDK tool, without executing PEs. #>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string[]] $Path,
    [string] $ManifestTool = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $ManifestTool) {
    $ManifestTool = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Windows Kits/10/bin/10.0.26100.0/x64/mt.exe'
}
if (-not [IO.File]::Exists($ManifestTool)) { throw 'Pinned SDK manifest tool is unavailable' }
if ($Path.Count -eq 0) { throw 'Name the frozen Brain and built Bridge executables' }
$working = Join-Path ([IO.Path]::GetTempPath()) ('ofca-pe-manifests-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($working) | Out-Null
$records = [Collections.Generic.List[object]]::new()
try {
    foreach ($candidate in $Path) {
        $file = [IO.Path]::GetFullPath($candidate)
        if (-not [IO.File]::Exists($file) -or [IO.Path]::GetExtension($file).ToLowerInvariant() -ne '.exe') {
            throw 'PE manifest verification requires executable files'
        }
        $output = Join-Path $working ([Guid]::NewGuid().ToString('N') + '.xml')
        $messages = & $ManifestTool -nologo "-inputresource:$file;#1" "-out:$output" 2>&1
        if ($LASTEXITCODE -ne 0 -or -not [IO.File]::Exists($output)) { throw 'SDK could not read the embedded application manifest' }
        if ([IO.FileInfo]::new($output).Length -gt 262144) { throw 'Embedded manifest exceeds its bound' }
        $settings = [Xml.XmlReaderSettings]::new()
        $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
        $settings.XmlResolver = $null
        $reader = [Xml.XmlReader]::Create($output, $settings)
        $document = [Xml.XmlDocument]::new()
        $document.XmlResolver = $null
        try { $document.Load($reader) } finally { $reader.Dispose() }
        $namespaces = [Xml.XmlNamespaceManager]::new($document.NameTable)
        $namespaces.AddNamespace('v3', 'urn:schemas-microsoft-com:asm.v3')
        $namespaces.AddNamespace('settings', 'http://schemas.microsoft.com/SMI/2016/WindowsSettings')
        $aware = $document.SelectNodes('//v3:application/v3:windowsSettings/settings:longPathAware', $namespaces)
        $privileges = $document.SelectNodes('//v3:requestedExecutionLevel', $namespaces)
        if ($aware.Count -ne 1 -or $aware[0].InnerText -cne 'true' -or $privileges.Count -ne 1 -or
            $privileges[0].GetAttribute('level') -cne 'asInvoker' -or $privileges[0].GetAttribute('uiAccess') -cne 'false') {
            throw 'Executable must embed longPathAware and ordinary asInvoker privileges'
        }
        $hasher = [Security.Cryptography.SHA256]::Create()
        $stream = [IO.File]::OpenRead($output)
        try { $digest = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
        finally { $hasher.Dispose(); $stream.Dispose() }
        $records.Add([ordered]@{ executable = [IO.Path]::GetFileName($file); embedded_manifest_sha256 = $digest; long_path_aware = $true; execution_level = 'asInvoker' })
    }
    [ordered]@{ schema = 'ofca-pe-manifests/v1'; executables = $records.ToArray() } | ConvertTo-Json -Depth 6
} finally {
    # This directory is created here from a GUID; it contains only extracted
    # inspection XML. Native PowerShell stays in control of its cleanup.
    if ([IO.Path]::GetFullPath($working).StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()), [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $working -Recurse -Force
    }
}
