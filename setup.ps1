<#
 Spejari for Windows - setup
   setup.bat                 create .venv and install the dependencies
   setup.bat -NoGui          CLI only (skips CustomTkinter)
   setup.bat -Dev            also install pytest
#>
param(
    [switch]$NoGui,
    [switch]$Dev
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Say($msg) { Write-Host "[spejari] $msg" -ForegroundColor Cyan }

# find Python 3.9+
$python = $null
foreach ($candidate in @(@("py", "-3"), @("python"))) {
    $exe = $candidate[0]
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $ver = & $exe @($candidate[1..($candidate.Length - 1)]) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$ver -ge [version]"3.9") { $python = $candidate; break }
    }
}
if (-not $python) {
    Write-Host "Python 3.9 or newer was not found." -ForegroundColor Red
    Write-Host "Install it from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH' and keep 'tcl/tk' selected)."
    exit 1
}
$pyExe = $python[0]
$pyArgs = @()
if ($python.Length -gt 1) { $pyArgs = $python[1..($python.Length - 1)] }

Say "Creating virtual environment (.venv)"
& $pyExe @pyArgs -m venv .venv
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip | Out-Null

if ($NoGui) {
    Say "Installing dependencies (CLI only)"
    $tmp = Join-Path $PSScriptRoot ".requirements-cli.tmp"
    Get-Content requirements.txt | Where-Object { $_ -notmatch "customtkinter" } | Set-Content $tmp
    & $venvPy -m pip install -r $tmp
    Remove-Item $tmp -Force
} else {
    Say "Installing dependencies"
    & $venvPy -m pip install -r requirements.txt
}
if ($Dev) { & $venvPy -m pip install pytest }

New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "exports") | Out-Null
Say "Done. Start Spejari with:  spejari.bat   (--cli for the terminal, --gui for the window, --simulate to try it)"
Say "Tip: spejari.bat --list-adapters  and  spejari.bat --list-gps  show what Spejari can see."
