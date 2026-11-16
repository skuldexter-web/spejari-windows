<#
 EXPERIMENTAL: build a single-file spejari.exe with PyInstaller.
   powershell -ExecutionPolicy Bypass -File build_exe.ps1
 The exe is written to dist\spejari.exe. config\spejari.toml is read from next to the exe if you copy it there.
#>
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Write-Host "Run setup.bat first." -ForegroundColor Red; exit 1 }
& $venvPy -m pip install pyinstaller
& $venvPy -m PyInstaller --noconfirm --onefile --name spejari --collect-all customtkinter `
    --hidden-import serial.tools.list_ports_windows main.py
Write-Host "Built dist\spejari.exe" -ForegroundColor Green
