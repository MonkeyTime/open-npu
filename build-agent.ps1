$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
Push-Location $PSScriptRoot
try {
    & $python -m PyInstaller --noconfirm --onedir --windowed --name OpenNpuAgent --collect-all openvino --hidden-import pystray._win32 agent.py
    if ($LASTEXITCODE -ne 0) { throw "La compilation de l'agent a échoué." }
} finally {
    Pop-Location
}
