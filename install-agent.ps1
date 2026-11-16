param(
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'OPEN-NPU'),
    [string]$AgentDirectory,
    [string]$EnrollmentFile = (Join-Path $PSScriptRoot 'enrollment.json'),
    [ValidateRange(0, 100000)][double]$UploadLimitMbps,
    [switch]$NoStartup
)
$ErrorActionPreference = 'Stop'
function Save-AgentSettings($Path, $Settings) {
    $temporary = "$Path.new"
    $Settings | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding UTF8
    if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $Path) }
}

if (!$AgentDirectory) {
    $AgentDirectory = Join-Path $PSScriptRoot 'OpenNpuAgent'
    if (!(Test-Path -LiteralPath $AgentDirectory)) {
        $AgentDirectory = Join-Path $PSScriptRoot 'dist/OpenNpuAgent'
    }
}
$source = (Resolve-Path -LiteralPath $AgentDirectory).Path
if (!(Test-Path -LiteralPath (Join-Path $source 'OpenNpuAgent.exe'))) {
    throw "Exécutable OpenNpuAgent introuvable."
}

$install = [IO.Path]::GetFullPath($InstallDirectory)
$destination = Join-Path $install 'agent'
$exe = Join-Path $destination 'OpenNpuAgent.exe'
$config = Join-Path $install 'node.json'
$setLimit = $PSBoundParameters.ContainsKey('UploadLimitMbps')
if ($setLimit -and $UploadLimitMbps -gt 0 -and $UploadLimitMbps -lt 0.05) {
    throw "Le plafond minimal est de 0,05 Mbit/s. Utilisez 0 pour aucun plafond."
}
$configuration = $null
if (Test-Path -LiteralPath $config) {
    $configuration = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
}
if (Test-Path -LiteralPath $EnrollmentFile) {
    $bootstrap = Get-Content -LiteralPath $EnrollmentFile -Raw | ConvertFrom-Json
} elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'runtime/enrollment.key')) {
    $bootstrap = @{
        server = 'http://127.0.0.1:8787'
        enrollment_key = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime/enrollment.key') -Raw).Trim()
    }
} elseif (!$configuration) {
    throw "Le fichier d’inscription enrollment.json est introuvable."
}
if ($bootstrap) {
    $origin = [Uri]$bootstrap.server
    if (!$origin.IsAbsoluteUri -or !$origin.Host -or $origin.UserInfo -or $origin.Query -or $origin.Fragment -or
            ($origin.Scheme -ne 'https' -and !($origin.Scheme -eq 'http' -and $origin.IsLoopback)) -or
            [string]::IsNullOrWhiteSpace($bootstrap.enrollment_key)) {
        throw "Configuration d’inscription invalide."
    }
    if ($configuration -and $configuration.server.TrimEnd('/') -ne $bootstrap.server.TrimEnd('/')) {
        throw "L’installation existante est liée à un autre serveur."
    }
}

$null = New-Item -ItemType Directory -Force -Path $destination
Get-Process OpenNpuAgent -ErrorAction SilentlyContinue | Where-Object Path -eq $exe | ForEach-Object {
    Stop-Process -Id $_.Id
    $_.WaitForExit()
}
if ($source -ne $destination) {
    Copy-Item -Path (Join-Path $source '*') -Destination $destination -Recurse -Force
}
if (!$configuration -or ($configuration.enrollment_key -and $bootstrap)) {
    $settings = @{}
    if ($configuration) {
        foreach ($property in $configuration.PSObject.Properties) {
            $settings[$property.Name] = $property.Value
        }
    }
    if (!$configuration -or ($configuration.enrollment_key -and $bootstrap)) {
        $settings.server = $bootstrap.server
        $settings.enrollment_key = $bootstrap.enrollment_key
    }
    Save-AgentSettings $config $settings
}
if ($setLimit) {
    $preferencesPath = Join-Path $install 'node.preferences.json'
    $preferences = @{ paused = $false }
    if (Test-Path -LiteralPath $preferencesPath) {
        $saved = Get-Content -LiteralPath $preferencesPath -Raw | ConvertFrom-Json
        $preferences.paused = $saved.paused -eq $true
    }
    $preferences.upload_limit_mbps = if ($UploadLimitMbps -gt 0) { $UploadLimitMbps } else { $null }
    Save-AgentSettings $preferencesPath $preferences
}

$arguments = '--config "{0}"' -f $config
if (!$NoStartup) {
    $startup = [Environment]::GetFolderPath('Startup')
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut((Join-Path $startup 'OPEN-NPU.lnk'))
    $shortcut.TargetPath = $exe
    $shortcut.Arguments = $arguments
    $shortcut.WorkingDirectory = $install
    $shortcut.Save()
}
$process = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $install -WindowStyle Hidden -PassThru
Write-Output "OPEN-NPU démarré en arrière-plan (PID $($process.Id)). Journal : $install\agent.log"
