$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$server = Join-Path $PSScriptRoot 'server.py'
$port = if ($env:OPEN_NPU_PORT) { $env:OPEN_NPU_PORT } else { '8787' }
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    throw "Le port $port est déjà utilisé."
}
$process = Start-Process -FilePath $python -ArgumentList ('"{0}"' -f $server) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if ($process.HasExited) { throw "Le serveur s'est arrêté. Vérifiez le port et la configuration." }
    try {
        $null = Invoke-WebRequest "http://127.0.0.1:$port/" -TimeoutSec 1
        $ready = $true
        break
    } catch {
        Start-Sleep -Milliseconds 250
    }
}
if (!$ready) {
    Stop-Process -Id $process.Id
    throw "Le serveur n'a pas répondu."
}
$listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen
Write-Output "Portail http://127.0.0.1:$port/ | PID $($listener.OwningProcess)"
