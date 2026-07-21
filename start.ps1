# R2 Manager – Startup Script (uv)
# Automatically creates a virtual environment, installs dependencies, and launches the app.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# ── 1. Ensure uv is available ─────────────────────────────────────────────────
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[启动] 未找到 uv，正在通过 pip 安装…" -ForegroundColor Yellow
    python -m pip install uv --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Error "安装 uv 失败。请手动安装：https://docs.astral.sh/uv/"
        exit 1
    }
}

# uv.exe may land in a Scripts folder that isn't on PATH (e.g. a per-user
# install). Fall back to invoking it as a Python module in that case.
if (Get-Command uv -ErrorAction SilentlyContinue) {
    $UvCmd = @("uv")
} else {
    $UvCmd = @("python", "-m", "uv")
}
$UvArgs = if ($UvCmd.Count -gt 1) {
    @($UvCmd[1..($UvCmd.Count - 1)])
} else {
    @()
}

# ── 2. Create virtual environment if it doesn't exist ────────────────────────
$VenvDir = Join-Path $ScriptDir ".venv"
if (-not (Test-Path $VenvDir)) {
    Write-Host "[启动] 正在使用 uv 创建虚拟环境…" -ForegroundColor Cyan
    & $UvCmd[0] @UvArgs venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Error "创建虚拟环境失败。"; exit 1 }
}

# ── 3. Install / sync dependencies ───────────────────────────────────────────
Write-Host "[启动] 正在安装依赖…" -ForegroundColor Cyan
& $UvCmd[0] @UvArgs pip install --python "$VenvDir\Scripts\python.exe" -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Error "安装依赖失败。"; exit 1 }

# ── 4. Launch the application ─────────────────────────────────────────────────
Write-Host "[启动] 正在启动 R2 管理器…" -ForegroundColor Green
& "$VenvDir\Scripts\python.exe" r2_manager.py
