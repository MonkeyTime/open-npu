param(
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'OPEN-NPU'),
    [string]$AgentDirectory,
    [string]$EnrollmentFile = (Join-Path $PSScriptRoot 'enrollment.json'),
    [ValidateRange(0, 100000)][double]$UploadLimitMbps,
    [ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$PublisherSha256,
    [ValidatePattern('^[A-Fa-f0-9]{64}$')][string]$CapsuleKeySha256,
    [switch]$NoStartup,
    [switch]$Update
)
$ErrorActionPreference = 'Stop'
function Get-BytesHash([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { [BitConverter]::ToString($sha.ComputeHash($Bytes)).Replace('-', '') }
    finally { $sha.Dispose() }
}

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
$installKey = Get-BytesHash ([Text.Encoding]::UTF8.GetBytes($install.TrimEnd([char]92, [char]47).ToUpperInvariant()))
$mutex = [Threading.Mutex]::new($false, "Local\OpenNpuInstall-$installKey")
$held = $false
try {
    try { $held = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $held = $true }
    if (!$held) { throw 'Another agent installation or update is already in progress.' }

    $destination = Join-Path $install 'agent'
    $exe = Join-Path $destination 'OpenNpuAgent.exe'
    $config = Join-Path $install 'node.json'
    $installedRelease = Join-Path $install 'agent-release.json'
    $updateTrust = Join-Path $install 'agent-update-trust.json'
    $releasePath = Join-Path $PSScriptRoot 'release.json'
    if (Test-Path -LiteralPath $releasePath) {
        $releaseBytes = [IO.File]::ReadAllBytes($releasePath)
        $release = [Text.Encoding]::UTF8.GetString($releaseBytes).TrimStart([char]0xfeff) | ConvertFrom-Json
        if ($release.version -notmatch '^\d{1,4}\.\d{1,4}\.\d{1,4}$') { throw 'Invalid release version.' }
    }
    $packageKey = Join-Path $PSScriptRoot 'capsule-signing.pub'
    $installedKey = Join-Path $install 'capsule-signing.pub'
    if (Test-Path -LiteralPath $packageKey) {
        $keyFileBytes = [IO.File]::ReadAllBytes($packageKey)
        $publishedKey = [Text.Encoding]::UTF8.GetString($keyFileBytes).TrimStart([char]0xfeff).Trim()
        if ($publishedKey -cnotmatch '^[0-9a-f]{64}$') { throw 'Cle publique de capsule invalide.' }
        if ((Test-Path -LiteralPath $installedKey) -and (Get-Content -LiteralPath $installedKey -Raw).Trim() -cne $publishedKey) {
            throw 'Cle de capsule differente. La rotation exige une validation de la cle publique.'
        }
    }
    $setLimit = $PSBoundParameters.ContainsKey('UploadLimitMbps')
    if ($setLimit -and $UploadLimitMbps -gt 0 -and $UploadLimitMbps -lt 0.05) {
        throw "Le plafond minimal est de 0,05 Mbit/s. Utilisez 0 pour aucun plafond."
    }
    $configuration = $null
    if (Test-Path -LiteralPath $config) {
        $configuration = Get-Content -LiteralPath $config -Raw | ConvertFrom-Json
    }
    if ($configuration -and (Test-Path -LiteralPath $updateTrust)) {
        $trustedPublisher = (Get-Content -LiteralPath $updateTrust -Raw | ConvertFrom-Json).publisher_sha256
        if ($trustedPublisher -notmatch '^[A-Fa-f0-9]{64}$' -or
            ($PublisherSha256 -and $PublisherSha256 -ne $trustedPublisher)) {
            throw 'The installed publisher certificate fingerprint does not match.'
        }
        $PublisherSha256 = $trustedPublisher
    }
    if (!$Update -and $release -and $configuration -and !$configuration.enrollment_key -and !$configuration.public_enrollment) {
        throw 'Run this release with -Update to preserve the existing installation.'
    }
    if (!$Update -and $release -and (Test-Path -LiteralPath $installedRelease)) {
        $installed = Get-Content -LiteralPath $installedRelease -Raw | ConvertFrom-Json
        if ([version]$release.version -lt [version]$installed.version) { throw 'An older version cannot replace this installation.' }
    }
    if ($Update) {
        if (!$configuration -or !$release -or !(Test-Path -LiteralPath $exe) -or $source -eq $destination) {
            throw 'An existing installation and an extracted release package are required.'
        }
        if ($setLimit -or $PSBoundParameters.ContainsKey('EnrollmentFile') -or (Test-Path -LiteralPath $EnrollmentFile) -or $publishedKey) {
            throw 'Use a release package without enrollment data. An update preserves the installed configuration.'
        }
        if (Test-Path -LiteralPath $installedRelease) {
            $installed = Get-Content -LiteralPath $installedRelease -Raw | ConvertFrom-Json
            if ([version]$release.version -le [version]$installed.version) { throw 'This version is already installed or older.' }
            if ($installed.publisher_sha256) {
                if ($PublisherSha256 -and $PublisherSha256 -ne $installed.publisher_sha256) {
                    throw 'The installed publisher certificate fingerprint does not match.'
                }
                $PublisherSha256 = $installed.publisher_sha256
            }
        }
    } elseif (Test-Path -LiteralPath $EnrollmentFile) {
        $enrollmentBytes = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $EnrollmentFile).Path)
        $bootstrap = [Text.Encoding]::UTF8.GetString($enrollmentBytes).TrimStart([char]0xfeff) | ConvertFrom-Json
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
        $publicEnrollment = $bootstrap.public_enrollment -is [bool] -and $bootstrap.public_enrollment
        if (!$origin.IsAbsoluteUri -or !$origin.Host -or $origin.UserInfo -or $origin.Query -or $origin.Fragment -or
                ($origin.Scheme -ne 'https' -and !($origin.Scheme -eq 'http' -and $origin.IsLoopback)) -or
                ($publicEnrollment -eq (![string]::IsNullOrWhiteSpace($bootstrap.enrollment_key)))) {
            throw "Configuration d’inscription invalide."
        }
        if ($configuration -and $configuration.server.TrimEnd('/') -ne $bootstrap.server.TrimEnd('/')) {
            throw "L’installation existante est liée à un autre serveur."
        }
    }
    $effectiveOrigin = [Uri]$(if ($bootstrap) { $bootstrap.server } else { $configuration.server })
    if (!$effectiveOrigin.IsAbsoluteUri) { throw 'Adresse de serveur invalide.' }
    $remote = !$effectiveOrigin.IsLoopback
    $manifest = $null
    if ($remote -or $PublisherSha256) {
        if (!$PublisherSha256) { throw 'Indiquez le SHA-256 du certificat de signature via -PublisherSha256.' }
        $expectedSource = Join-Path $PSScriptRoot 'OpenNpuAgent'
        $expectedEnrollment = Join-Path $PSScriptRoot 'enrollment.json'
        if ($source -ne $expectedSource -or (!$Update -and (!(Test-Path -LiteralPath $expectedEnrollment) -or
            (Resolve-Path -LiteralPath $EnrollmentFile).Path -ne $expectedEnrollment))) {
            throw 'Le binaire et la configuration doivent provenir du paquet signé.'
        }
        $manifest = & (Join-Path $PSScriptRoot 'verify-agent-package.ps1') -Package $PSScriptRoot -PublisherSha256 $PublisherSha256 -PassThru
        if (($release -and (Get-BytesHash $releaseBytes) -ne $manifest['release.json']) -or
            (!$Update -and (!$enrollmentBytes -or (Get-BytesHash $enrollmentBytes) -ne $manifest['enrollment.json'])) -or
            ($null -ne $keyFileBytes) -ne $manifest.ContainsKey('capsule-signing.pub')) {
            throw 'Les fichiers de configuration lus ne correspondent pas au manifeste.'
        }
        if ($publishedKey) {
            if ((Get-BytesHash $keyFileBytes) -ne $manifest['capsule-signing.pub']) { throw 'Fichier de cle modifie.' }
            if (!$CapsuleKeySha256) { throw 'Indiquez le SHA-256 indépendant de la clé de capsule via -CapsuleKeySha256.' }
            $keyBytes = [byte[]]$(for ($index=0; $index -lt 64; $index+=2) {
                [Convert]::ToByte($publishedKey.Substring($index, 2), 16)
            })
            if ((Get-BytesHash $keyBytes) -ne $CapsuleKeySha256) { throw 'Empreinte de la cle de capsule differente.' }
        }
    }

    $null = New-Item -ItemType Directory -Force -Path $install
    $staged = Join-Path $install ('.agent-new-' + [Guid]::NewGuid().ToString('N'))
    if ($source -ne $destination) {
        Copy-Item -LiteralPath $source -Destination $staged -Recurse -Force
        if ($manifest) {
            $files = @(Get-ChildItem -LiteralPath $staged -Recurse -Force -File)
            $expected = @($manifest.Keys | Where-Object { $_.StartsWith('OpenNpuAgent/') })
            if ($files.Count -ne $expected.Count) { throw 'Copie incomplète.' }
            foreach ($file in $files) {
                $relative = 'OpenNpuAgent/' + $file.FullName.Substring($staged.Length + 1).Replace('\','/')
                if ((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne $manifest[$relative]) {
                    throw "Fichier installé altéré : $relative"
                }
            }
        }
    }
    if ($publishedKey -and !(Test-Path -LiteralPath $installedKey)) {
        [IO.File]::WriteAllText($installedKey, $publishedKey)
    }
    $arguments = '--config "{0}"' -f $config
    if ($Update) {
        $probeLog = Join-Path $staged 'update-probe.log'
        $probe = Start-Process -FilePath (Join-Path $staged 'OpenNpuAgent.exe') -ArgumentList ('--probe --log "{0}"' -f $probeLog) -WorkingDirectory $install -WindowStyle Hidden -PassThru
        if (!$probe.WaitForExit(180000)) {
            Stop-Process -InputObject $probe -ErrorAction SilentlyContinue
            throw "The new agent did not pass its NPU self-test. See $probeLog."
        }
        if ($probe.ExitCode -ne 0) { throw "The new agent did not pass its NPU self-test. See $probeLog." }
        if (Test-Path -LiteralPath $probeLog) { Remove-Item -LiteralPath $probeLog }
    }
    Get-Process OpenNpuAgent -ErrorAction SilentlyContinue | Where-Object Path -eq $exe | ForEach-Object {
        $running = $_
        try { Stop-Process -InputObject $running -ErrorAction Stop }
        catch { if (!$running.HasExited) { throw } }
        $running.WaitForExit()
    }
    if ($source -ne $destination) {
        if (Test-Path -LiteralPath $destination) {
            $previous = Join-Path $install ('.agent-previous-' + [Guid]::NewGuid().ToString('N'))
            [IO.Directory]::Move($destination, $previous)
        }
        try { [IO.Directory]::Move($staged, $destination) }
        catch {
            if ($previous) { [IO.Directory]::Move($previous, $destination) }
            throw
        }
    }
    if (!$configuration -or (($configuration.enrollment_key -or $configuration.public_enrollment) -and $bootstrap)) {
        $settings = @{}
        if ($configuration) {
            foreach ($property in $configuration.PSObject.Properties) {
                $settings[$property.Name] = $property.Value
            }
        }
        $settings.server = $bootstrap.server
        if ($publicEnrollment) {
            $settings.Remove('enrollment_key')
            $settings.public_enrollment = $true
        } else {
            $settings.Remove('public_enrollment')
            $settings.enrollment_key = $bootstrap.enrollment_key
        }
        Save-AgentSettings $config $settings
    }
    if ($setLimit) {
        $preferencesPath = Join-Path $install 'node.preferences.json'
        $preferences = @{ paused = $false }
        if (Test-Path -LiteralPath $preferencesPath) {
            $saved = Get-Content -LiteralPath $preferencesPath -Raw | ConvertFrom-Json
            foreach ($property in $saved.PSObject.Properties) {
                $preferences[$property.Name] = $property.Value
            }
        }
        $preferences.upload_limit_mbps = if ($UploadLimitMbps -gt 0) { $UploadLimitMbps } else { $null }
        Save-AgentSettings $preferencesPath $preferences
    }

    if (!$NoStartup -and !$Update) {
        $startup = [Environment]::GetFolderPath('Startup')
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut((Join-Path $startup 'OPEN-NPU.lnk'))
        $shortcut.TargetPath = $exe
        $shortcut.Arguments = $arguments
        $shortcut.WorkingDirectory = $install
        $shortcut.Save()
    }
    try {
        $process = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $install -WindowStyle Hidden -PassThru
        if ($Update -and $process.WaitForExit(1500)) { throw 'The updated agent exited during startup.' }
    } catch {
        if ($Update -and $previous) {
            [IO.Directory]::Move($destination, $staged)
            [IO.Directory]::Move($previous, $destination)
            Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $install -WindowStyle Hidden
        }
        throw
    }
    if ($release) {
        Save-AgentSettings $installedRelease @{version = $release.version; publisher_sha256 = $PublisherSha256}
    }
    if ($manifest -and $PublisherSha256) {
        Save-AgentSettings $updateTrust @{publisher_sha256 = $PublisherSha256}
    }
    Write-Output "OPEN-NPU démarré en arrière-plan (PID $($process.Id)). Journal : $install\agent.log"
} finally {
    if ($held) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
