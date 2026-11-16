param([Parameter(Mandatory = $true)][string]$Server, [string]$InvitationFile)
$ErrorActionPreference = 'Stop'
$origin = [Uri]$Server
if (!$origin.IsAbsoluteUri -or !$origin.Host -or $origin.UserInfo -or $origin.Query -or $origin.Fragment -or
        ($origin.Scheme -ne 'https' -and !($origin.Scheme -eq 'http' -and $origin.IsLoopback))) {
    throw "Utilisez une URL HTTPS ou l’adresse locale du serveur."
}
$source = Join-Path $PSScriptRoot 'dist/OpenNpuAgent'
if (!(Test-Path -LiteralPath (Join-Path $source 'OpenNpuAgent.exe'))) { throw "Exécutez build-agent.ps1 d’abord." }
if ($InvitationFile) {
    $key = (Get-Content -LiteralPath $InvitationFile -Raw | ConvertFrom-Json).enrollment_key
} elseif ($origin.IsLoopback) {
    $key = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime/enrollment.key') -Raw).Trim()
} else {
    throw "Fournissez le fichier JSON retourné par l’API de création d’invitation avec -InvitationFile."
}
if ([string]::IsNullOrWhiteSpace($key)) { throw "Clé d’inscription manquante." }
$package = Join-Path $PSScriptRoot ('dist/OpenNpuSetup-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$null = New-Item -ItemType Directory -Path $package
Copy-Item -LiteralPath $source -Destination $package -Recurse
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'install-agent.ps1') -Destination $package
@{ server = $Server.TrimEnd('/'); enrollment_key = $key } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $package 'enrollment.json') -Encoding UTF8
Write-Output "Dossier d’installation à remettre aux participants : $package"
