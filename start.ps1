# R2 Manager – Startup Script (uv)
# Automatically creates a virtual environment, installs dependencies, and launches the app.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# ── 1. Ensure uv is available ─────────────────────────────────────────────────
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[start] uv not found – installing via pip..." -ForegroundColor Yellow
    pip install uv --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install uv. Please install it manually: https://docs.astral.sh/uv/"
        exit 1
    }
}

# ── 2. Create virtual environment if it doesn't exist ────────────────────────
$VenvDir = Join-Path $ScriptDir ".venv"
if (-not (Test-Path $VenvDir)) {
    Write-Host "[start] Creating virtual environment with uv..." -ForegroundColor Cyan
    uv venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Error "uv venv failed."; exit 1 }
}

# ── 3. Install / sync dependencies ───────────────────────────────────────────
Write-Host "[start] Installing dependencies..." -ForegroundColor Cyan
uv pip install --python "$VenvDir\Scripts\python.exe" -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Error "Dependency installation failed."; exit 1 }

# ── 4. Launch the application ─────────────────────────────────────────────────
Write-Host "[start] Starting R2 Manager..." -ForegroundColor Green
& "$VenvDir\Scripts\python.exe" r2_manager.py
