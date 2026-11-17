param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$')][string]$Version,
    [uri]$Server,
    [ValidatePattern('^[A-Fa-f0-9]{40}$')][string]$SignerThumbprint,
    [uri]$TimestampServer,
    [string]$RuntimeDirectory = (Join-Path $PSScriptRoot 'runtime')
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$releaseRoot = Join-Path $RuntimeDirectory 'agent-releases'
$archive = Join-Path $releaseRoot "OpenNpuAgent-$Version-windows-x64.zip"
$name = "OPEN-NPU-Setup-$Version-windows-x64"
$signed = [bool]$Server
if (($signed -and (!$SignerThumbprint -or !$TimestampServer)) -or (!$signed -and ($SignerThumbprint -or $TimestampServer))) {
    throw 'Supply -Server, -SignerThumbprint and -TimestampServer together.'
}
if ($signed -and (!$Server.IsAbsoluteUri -or $Server.Scheme -ne 'https' -or !$Server.Host -or
        $Server.UserInfo -or $Server.Query -or $Server.Fragment -or $Server.AbsolutePath -ne '/' -or
        !$TimestampServer.IsAbsoluteUri -or $TimestampServer.Scheme -ne 'https')) {
    throw 'Use HTTPS for the instance origin and timestamp service.'
}
if ($signed) { $name += '-signed' }

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($archive)
try {
    if ([bool]$zip.GetEntry('package-manifest.p7s') -ne $signed) { throw 'The installer and release ZIP must use the same signing mode.' }
    $reader = [IO.StreamReader]::new($zip.GetEntry('release.json').Open())
    try { $metadata = $reader.ReadToEnd() | ConvertFrom-Json }
    finally { $reader.Dispose() }
    if ($metadata.version -ne $Version) { throw 'Unexpected version in the release archive.' }
} finally { $zip.Dispose() }

$build = Join-Path $PSScriptRoot "build/$name"
$null = New-Item -ItemType Directory -Force -Path $build
$embedded = Join-Path $build 'release.zip'
$resources = @('--add-data', 'install-agent.ps1:.', '--add-data', 'verify-agent-package.ps1:.')
if ($signed) {
    $certificate = Get-Item -LiteralPath "Cert:\CurrentUser\My\$SignerThumbprint"
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $fingerprint = [BitConverter]::ToString($sha.ComputeHash($certificate.RawData)).Replace('-', '') }
    finally { $sha.Dispose() }
    $package = Join-Path $build ('public-package-' + [Guid]::NewGuid().ToString('N'))
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $package)
    & (Join-Path $PSScriptRoot 'verify-agent-package.ps1') -Package $package -PublisherSha256 $fingerprint
    @{server = $Server.GetLeftPart([UriPartial]::Authority); public_enrollment = $true} |
        ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $package 'enrollment.json') -Encoding UTF8
    & (Join-Path $PSScriptRoot 'sign-agent-package.ps1') -Package $package -SignerThumbprint $SignerThumbprint -TimestampServer $TimestampServer
    if (Test-Path -LiteralPath $embedded) { Remove-Item -LiteralPath $embedded }
    [IO.Compression.ZipFile]::CreateFromDirectory($package, $embedded)
    $descriptor = Join-Path $build 'setup.json'
    @{server = $Server.GetLeftPart([UriPartial]::Authority); publisher_sha256 = $fingerprint} |
        ConvertTo-Json -Compress | Set-Content -LiteralPath $descriptor -Encoding UTF8
    $resources += @('--add-data', "${descriptor}:.")
} else {
    Copy-Item -LiteralPath $archive -Destination $embedded -Force
}
Push-Location $PSScriptRoot
try {
    & $python -m PyInstaller --clean --noconfirm --onefile --windowed --name $name --distpath $build --workpath (Join-Path $build 'work') --add-data "${embedded}:." @resources windows_installer.py
    if ($LASTEXITCODE -ne 0) { throw 'Failed to build the Windows installer.' }
    $exe = Join-Path $build "$name.exe"
    if ($signed) {
        $signature = Set-AuthenticodeSignature -LiteralPath $exe -Certificate $certificate -HashAlgorithm SHA256 -TimestampServer $TimestampServer.AbsoluteUri
        if ($signature.Status -ne 'Valid' -or !$signature.TimeStamperCertificate) { throw 'Installer signature or timestamp is invalid.' }
    }
    $pending = Join-Path $releaseRoot ('.setup-' + [Guid]::NewGuid().ToString('N'))
    [IO.File]::Copy($exe, $pending)
    [IO.File]::Move($pending, (Join-Path $releaseRoot "$name.exe"))
    Write-Output "Published $name.exe"
} finally { Pop-Location }
