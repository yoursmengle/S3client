# R2 Manager – Build Script
# Compiles r2_manager.py into a single standalone Windows .exe using PyInstaller.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# ── 1. Ensure uv is available ─────────────────────────────────────────────────
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[build] uv not found – installing via pip..." -ForegroundColor Yellow
    python -m pip install uv --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install uv. Please install it manually: https://docs.astral.sh/uv/"
        exit 1
    }
}

# uv.exe may land in a Scripts folder that isn't on PATH. Fall back to
# invoking it as a Python module in that case.
if (Get-Command uv -ErrorAction SilentlyContinue) {
    $UvCmd = @("uv")
} else {
    $UvCmd = @("python", "-m", "uv")
}

# ── 2. Create virtual environment if it doesn't exist ────────────────────────
$VenvDir = Join-Path $ScriptDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvDir)) {
    Write-Host "[build] Creating virtual environment with uv..." -ForegroundColor Cyan
    & $UvCmd[0] $UvCmd[1..($UvCmd.Length - 1)] venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Error "uv venv failed."; exit 1 }
}

# ── 3. Install dependencies + PyInstaller ─────────────────────────────────────
Write-Host "[build] Installing dependencies and PyInstaller..." -ForegroundColor Cyan
$env:UV_HTTP_TIMEOUT = "120"
& $UvCmd[0] $UvCmd[1..($UvCmd.Length - 1)] pip install --python $VenvPython -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { Write-Error "Dependency installation failed."; exit 1 }

# ── 4. Build the standalone executable ────────────────────────────────────────
Write-Host "[build] Building R2Manager.exe with PyInstaller..." -ForegroundColor Cyan
& $VenvPython -m PyInstaller `
    --name "R2Manager" `
    --onefile `
    --windowed `
    --noconfirm `
    --clean `
    r2_manager.py
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller build failed."; exit 1 }

Write-Host "[build] Done. Executable created at: dist\R2Manager.exe" -ForegroundColor Green
