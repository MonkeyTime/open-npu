$ErrorActionPreference = 'Stop'
$agent = Join-Path $PSScriptRoot 'dist/OpenNpuAgent/OpenNpuAgent.exe'
$config = Join-Path $PSScriptRoot 'runtime/node.json'
if (!(Test-Path -LiteralPath $config)) {
    $keyFile = Join-Path $PSScriptRoot 'runtime/enrollment.key'
    if (!(Test-Path -LiteralPath $keyFile)) { throw "Démarrez d’abord le serveur local." }
    $port = if ($env:OPEN_NPU_PORT) { $env:OPEN_NPU_PORT } else { '8787' }
    @{ server = "http://127.0.0.1:$port"; enrollment_key = (Get-Content -LiteralPath $keyFile -Raw).Trim() } |
        ConvertTo-Json | Set-Content -LiteralPath $config -Encoding UTF8
}
$process = Start-Process -FilePath $agent -ArgumentList ('--config "{0}"' -f $config) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
Write-Output "Agent OPEN-NPU | PID $($process.Id)"
