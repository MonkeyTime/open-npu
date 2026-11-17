param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$')][string]$Version,
    [string]$AgentDirectory = (Join-Path $PSScriptRoot 'dist/OpenNpuAgent'),
    [string]$RuntimeDirectory = $(if ($env:OPEN_NPU_RUNTIME) { $env:OPEN_NPU_RUNTIME } else { Join-Path $PSScriptRoot 'runtime' }),
    [ValidatePattern('^[A-Fa-f0-9]{40}$')][string]$SignerThumbprint,
    [uri]$TimestampServer
)

$ErrorActionPreference = 'Stop'
if ([bool]$SignerThumbprint -ne [bool]$TimestampServer) { throw 'Supply both signing options together.' }
if ($env:OPEN_NPU_MODE -eq 'pilot' -and !$SignerThumbprint) { throw 'A pilot release must be signed.' }
$source = (Resolve-Path -LiteralPath $AgentDirectory).Path
if (!(Test-Path -LiteralPath (Join-Path $source 'OpenNpuAgent.exe') -PathType Leaf) -or
    !(Test-Path -LiteralPath (Join-Path $source '_internal') -PathType Container)) {
    throw 'The release requires the onedir agent build.'
}
$releaseRoot = Join-Path ([IO.Path]::GetFullPath($RuntimeDirectory)) 'agent-releases'
$null = New-Item -ItemType Directory -Force -Path $releaseRoot
$publication = [IO.File]::Open((Join-Path $releaseRoot '.publish.lock'), [IO.FileMode]::OpenOrCreate,
    [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    $name = "OpenNpuAgent-$Version-windows-x64.zip"
    $archive = Join-Path $releaseRoot $name
    $latest = Join-Path $releaseRoot 'latest.json'
    if ((Test-Path -LiteralPath $archive) -or ((Test-Path -LiteralPath $latest) -and
            [version]$Version -le [version](Get-Content -LiteralPath $latest -Raw | ConvertFrom-Json).version)) {
        throw 'Publish a higher version and keep existing release archives immutable.'
    }

    $id = [Guid]::NewGuid().ToString('N')
    $stage = Join-Path $releaseRoot ".staging-$id"
    $pendingZip = Join-Path $releaseRoot ".archive-$id"
    $pendingMetadata = Join-Path $releaseRoot ".latest-$id"
    $null = New-Item -ItemType Directory -Path $stage
    try {
        $packageAgent = Join-Path $stage 'OpenNpuAgent'
        $null = New-Item -ItemType Directory -Path $packageAgent
        Copy-Item -LiteralPath (Join-Path $source 'OpenNpuAgent.exe'),(Join-Path $source '_internal') -Destination $packageAgent -Recurse
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'install-agent.ps1'),(Join-Path $PSScriptRoot 'verify-agent-package.ps1') -Destination $stage
        @{version = $Version} | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $stage 'release.json') -Encoding UTF8
        if ($SignerThumbprint) {
            & (Join-Path $PSScriptRoot 'sign-agent-package.ps1') -Package $stage -SignerThumbprint $SignerThumbprint -TimestampServer $TimestampServer
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::CreateFromDirectory($stage, $pendingZip)
        $metadata = @{
            version = $Version
            signed = [bool]$SignerThumbprint
            sha256 = (Get-FileHash -LiteralPath $pendingZip -Algorithm SHA256).Hash.ToLowerInvariant()
            size = (Get-Item -LiteralPath $pendingZip).Length
        }
        $metadata | ConvertTo-Json -Compress | Set-Content -LiteralPath $pendingMetadata -Encoding UTF8
        [IO.File]::Move($pendingZip, $archive)
        if (Test-Path -LiteralPath $latest) { [IO.File]::Replace($pendingMetadata, $latest, [NullString]::Value) }
        else { [IO.File]::Move($pendingMetadata, $latest) }
        Write-Output "Published $archive"
    } finally {
        $resolvedStage = (Resolve-Path -LiteralPath $stage).Path
        if ($resolvedStage -eq (Join-Path $releaseRoot ".staging-$id")) {
            Remove-Item -LiteralPath $resolvedStage -Recurse -Force
        }
        foreach ($file in @($pendingZip, $pendingMetadata)) {
            if (Test-Path -LiteralPath $file -PathType Leaf) { Remove-Item -LiteralPath $file }
        }
    }
} finally {
    $publication.Dispose()
}
