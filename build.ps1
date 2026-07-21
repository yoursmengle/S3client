# R2 Manager – Build Script
# Compiles r2_manager.py into a single standalone Windows .exe using PyInstaller.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

# ── 1. Ensure uv is available ─────────────────────────────────────────────────
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[构建] 未找到 uv，正在通过 pip 安装…" -ForegroundColor Yellow
    python -m pip install uv --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Error "安装 uv 失败。请手动安装：https://docs.astral.sh/uv/"
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
$UvArgs = if ($UvCmd.Count -gt 1) {
    @($UvCmd[1..($UvCmd.Count - 1)])
} else {
    @()
}

# ── 2. Create virtual environment if it doesn't exist ────────────────────────
$VenvDir = Join-Path $ScriptDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvDir)) {
    Write-Host "[构建] 正在使用 uv 创建虚拟环境…" -ForegroundColor Cyan
    & $UvCmd[0] @UvArgs venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Error "创建虚拟环境失败。"; exit 1 }
}

# ── 3. Install dependencies + PyInstaller ─────────────────────────────────────
Write-Host "[构建] 正在安装依赖和 PyInstaller…" -ForegroundColor Cyan
$env:UV_HTTP_TIMEOUT = "120"
& $UvCmd[0] @UvArgs pip install --python $VenvPython -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { Write-Error "安装依赖失败。"; exit 1 }

# ── 4. Build the standalone executable ────────────────────────────────────────
Write-Host "[构建] 正在使用 PyInstaller 构建 R2Manager.exe…" -ForegroundColor Cyan
& $VenvPython -m PyInstaller `
    --name "R2Manager" `
    --onefile `
    --windowed `
    --noconfirm `
    --clean `
    r2_manager.py
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller 构建失败。"; exit 1 }

Write-Host "[构建] 完成。可执行文件已生成：dist\R2Manager.exe" -ForegroundColor Green
