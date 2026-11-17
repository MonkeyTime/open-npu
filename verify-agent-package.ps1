param(
    [Parameter(Mandatory = $true)][string]$Package,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$PublisherSha256,
    [switch]$PassThru
)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSEdition -eq 'Core') { Add-Type -AssemblyName System.Security.Cryptography.Pkcs }
else { Add-Type -AssemblyName System.Security }
$root = (Resolve-Path -LiteralPath $Package).Path
if ((Get-Item -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint) {
    throw 'Lien ou jonction a la racine du paquet.'
}
$manifestPath = Join-Path $root 'package-manifest.json'
$signaturePath = Join-Path $root 'package-manifest.p7s'
$bytes = [IO.File]::ReadAllBytes($manifestPath)
$content = [System.Security.Cryptography.Pkcs.ContentInfo]::new($bytes)
$cms = [System.Security.Cryptography.Pkcs.SignedCms]::new($content, $true)
$cms.Decode([IO.File]::ReadAllBytes($signaturePath))
if ($cms.SignerInfos.Count -ne 1 -or !$cms.SignerInfos[0].Certificate) { throw 'Signature de paquet invalide.' }
function Get-CertificateHash($Certificate) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { [BitConverter]::ToString($sha.ComputeHash($Certificate.RawData)).Replace('-', '') }
    finally { $sha.Dispose() }
}
if ((Get-CertificateHash $cms.SignerInfos[0].Certificate) -ne $PublisherSha256) { throw 'Editeur inattendu.' }
$cms.CheckSignature($false)
$manifest = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
if ($manifest.format -ne 1 -or !$manifest.files) { throw 'Manifeste non pris en charge.' }
$expected = @{}
foreach ($item in $manifest.files) {
    $relative = [string]$item.path
    if ($relative -notmatch '^[^/\\:]+(?:/[^/\\:]+)*$' -or $relative -match '(^|/)\.{1,2}(/|$)' -or
        $relative -match '[\x00-\x1f]' -or $item.sha256 -notmatch '^[A-Fa-f0-9]{64}$' -or $expected.ContainsKey($relative)) {
        throw 'Entree de manifeste invalide.'
    }
    $expected[$relative] = [string]$item.sha256
}
$all = @(Get-ChildItem -LiteralPath $root -Recurse -Force)
if ($all | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw 'Lien ou jonction dans le paquet.' }
$actual = @($all | Where-Object { !$_.PSIsContainer } | ForEach-Object {
    $_.FullName.Substring($root.Length + 1).Replace('\', '/')
} | Where-Object { $_ -notin ('package-manifest.json','package-manifest.p7s') })
if ($actual.Count -ne $expected.Count) { throw 'Le contenu du paquet ne correspond pas au manifeste.' }
foreach ($relative in $actual) {
    if (!$expected.ContainsKey($relative) -or (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $root $relative)).Hash -ne $expected[$relative]) {
        throw "Fichier absent du manifeste ou altere : $relative"
    }
}
foreach ($relative in @('OpenNpuAgent/OpenNpuAgent.exe', 'install-agent.ps1')) {
    if (!$expected.ContainsKey($relative)) { throw "Fichier requis absent : $relative" }
    $signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $root $relative)
    if ($signature.Status -ne 'Valid' -or !$signature.SignerCertificate -or !$signature.TimeStamperCertificate -or
            (Get-CertificateHash $signature.SignerCertificate) -ne $PublisherSha256) {
        throw "Signature Authenticode invalide ou non horodatee : $relative"
    }
}
if ($PassThru) { return $expected }
Write-Output 'Paquet Windows verifie.'
