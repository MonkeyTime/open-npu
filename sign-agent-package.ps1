param(
    [Parameter(Mandatory = $true)][string]$Package,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{40}$')][string]$SignerThumbprint,
    [Parameter(Mandatory = $true)][uri]$TimestampServer
)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -eq 'Core') { Add-Type -AssemblyName System.Security.Cryptography.Pkcs }
else { Add-Type -AssemblyName System.Security }
if (!$TimestampServer.IsAbsoluteUri -or $TimestampServer.Scheme -ne 'https') { throw 'Utilisez une URL HTTPS pour l horodatage.' }
$root = (Resolve-Path -LiteralPath $Package).Path
if ((Get-Item -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Lien ou jonction a la racine du paquet.'
}
$certificate = Get-Item -LiteralPath "Cert:\CurrentUser\My\$SignerThumbprint"
if (!$certificate.HasPrivateKey) { throw 'Cle privee de signature absente.' }
foreach ($relative in @('OpenNpuAgent/OpenNpuAgent.exe', 'install-agent.ps1')) {
    $signature = Set-AuthenticodeSignature -LiteralPath (Join-Path $root $relative) -Certificate $certificate -HashAlgorithm SHA256 -TimestampServer $TimestampServer.AbsoluteUri
    if ($signature.Status -ne 'Valid' -or !$signature.TimeStamperCertificate) { throw "Signature horodatee invalide : $relative" }
}
$all = @(Get-ChildItem -LiteralPath $root -Recurse -Force)
if ($all | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw 'Lien ou jonction dans le paquet.' }
$files = @($all | Where-Object { !$_.PSIsContainer } | Sort-Object FullName | ForEach-Object {
    $relative = $_.FullName.Substring($root.Length + 1).Replace('\', '/')
    if ($relative -notin ('package-manifest.json','package-manifest.p7s')) {
        @{ path=$relative; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash }
    }
})
$manifest = @{ format=1; files=$files } | ConvertTo-Json -Depth 4 -Compress
$bytes = [Text.Encoding]::UTF8.GetBytes($manifest)
$content = [System.Security.Cryptography.Pkcs.ContentInfo]::new($bytes)
$cms = [System.Security.Cryptography.Pkcs.SignedCms]::new($content, $true)
$signer = [System.Security.Cryptography.Pkcs.CmsSigner]::new($certificate)
$signer.DigestAlgorithm = [Security.Cryptography.Oid]::new('2.16.840.1.101.3.4.2.1')
$cms.ComputeSignature($signer)
[IO.File]::WriteAllBytes((Join-Path $root 'package-manifest.json'), $bytes)
[IO.File]::WriteAllBytes((Join-Path $root 'package-manifest.p7s'), $cms.Encode())
$sha = [Security.Cryptography.SHA256]::Create()
try { $fingerprint = [BitConverter]::ToString($sha.ComputeHash($certificate.RawData)).Replace('-', '') }
finally { $sha.Dispose() }
& (Join-Path $PSScriptRoot 'verify-agent-package.ps1') -Package $root -PublisherSha256 $fingerprint
Write-Output "Empreinte SHA-256 de l'editeur a transmettre par un canal de confiance : $fingerprint"
