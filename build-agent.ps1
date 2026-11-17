$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
Push-Location $PSScriptRoot
try {
    & $python -m py_compile agent.py agent_updates.py tray.py capsule_worker.py capsule_manifest.py
    if ($LASTEXITCODE -ne 0) { throw "Le code de l’agent ne compile pas." }
    & $python -m PyInstaller --clean --noconfirm --onedir --windowed --name OpenNpuAgent --collect-all openvino --hidden-import pystray._win32 --hidden-import capsule_worker --add-data "verify-agent-package.ps1:." agent.py
    if ($LASTEXITCODE -ne 0) { throw "La compilation de l'agent a échoué." }
} finally {
    Pop-Location
}
