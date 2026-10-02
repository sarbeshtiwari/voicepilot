#Requires -Version 5.1
<#
    voicepilot installer for Windows.

    Solo mode - voice control of the machine - runs natively here.
    Wrapping a terminal AI agent needs a Unix pty (pty/termios/fcntl), which
    Windows does not have; use WSL for that and run install.sh inside it.

    Usage:  powershell -ExecutionPolicy Bypass -File install.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

# --- 1. pick an interpreter -------------------------------------------------
# Newest is not safest: compiled deps (ctranslate2) lag behind new releases.
$pyExe = $null
$pyArgs = @()

if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in '3.12', '3.13', '3.11', '3.10') {
        & py "-$v" -c "import sys" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $pyExe = 'py'; $pyArgs = @("-$v"); break
        }
    }
}
if (-not $pyExe) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) {
        throw "No Python found. Install Python 3.12 from python.org (tick 'Add python.exe to PATH')."
    }
    $pyExe = $found.Source
    Write-Host "warning: using $(& $pyExe -V). If faster-whisper fails to install," -ForegroundColor Yellow
    Write-Host "         install Python 3.12 and re-run this script." -ForegroundColor Yellow
}
Write-Host "==> interpreter: $pyExe $pyArgs"

# --- 2. build the venv ------------------------------------------------------
if (Test-Path .venv) { Remove-Item -Recurse -Force .venv }
& $pyExe @pyArgs -m venv .venv
if ($LASTEXITCODE -ne 0) { throw "could not create the virtual environment" }

$venvPy = Join-Path $Here '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) { throw "venv created but $venvPy is missing" }

Write-Host "==> installing dependencies"
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }

# --- 3. a launcher on PATH --------------------------------------------------
# Windows ignores shebangs, so ship a .cmd shim instead of rewriting one.
$binDir = Join-Path $env:LOCALAPPDATA 'voicepilot'
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$shim = Join-Path $binDir 'voicepilot.cmd'
$script = "@echo off`r`n`"$venvPy`" `"$Here\voicepilot.py`" %*`r`n"
Set-Content -Path $shim -Value $script -Encoding ASCII -NoNewline
Write-Host "==> installed: $shim"

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$binDir*") {
    [Environment]::SetEnvironmentVariable('Path', "$userPath;$binDir", 'User')
    Write-Host "==> added $binDir to your PATH - open a NEW terminal to pick it up"
}

# --- 4. what actually works here -------------------------------------------
Write-Host ""
Write-Host "Done. Verify with:" -ForegroundColor Green
Write-Host "    voicepilot --check"
Write-Host "    voicepilot solo"
Write-Host ""
Write-Host "Note: wrapping an AI agent (voicepilot claude) needs a Unix pty and"
Write-Host "does not run on native Windows. Use WSL for that."
