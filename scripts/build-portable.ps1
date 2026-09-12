<#
.SYNOPSIS
    Builds the portable folder for Windows (spec section 17, D-09).

.DESCRIPTION
    Produces release/AI-Classroom-Live/ with this layout:

        AI-Classroom-Live.exe
        runtime/backend/aiclassroom-backend.exe
        data/            (config, materials, sessions, metrics, models)
        README.txt

    The folder can be copied to a USB stick and run without an installer, with
    no administrator rights and with neither Python nor Node.js on the machine.

.NOTES
    Requires, on the build machine only: Python 3.11+, Node.js 20+ and the Rust
    toolchain. None of these is needed on the classroom PC.
#>
[CmdletBinding()]
param(
    [string]$Configuration = "release",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$release = Join-Path $root "release"
$staging = Join-Path $release "AI-Classroom-Live"

Write-Host "== 1/6 Backend: entorno y dependencias ==" -ForegroundColor Cyan
Push-Location (Join-Path $root "backend")
try {
    if (-not (Test-Path ".venv")) { python -m venv .venv }
    & .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
    & .\.venv\Scripts\python.exe -m pip install -e ".[dev,wakeword]" pyinstaller --quiet

    if (-not $SkipTests) {
        Write-Host "== 2/6 Backend: tests ==" -ForegroundColor Cyan
        & .\.venv\Scripts\python.exe -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw "Los tests del backend han fallado." }
    }

    Write-Host "== 3/6 Backend: empaquetado con PyInstaller ==" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m PyInstaller aiclassroom-backend.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller ha fallado." }
}
finally { Pop-Location }

# Tauri looks for the sidecar under the exact target triple of the build host.
$triple = (rustc -vV | Select-String "^host:").ToString().Split(" ")[1]
$binaries = Join-Path $root "src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $binaries | Out-Null
Copy-Item `
    (Join-Path $root "backend\dist\backend\aiclassroom-backend.exe") `
    (Join-Path $binaries "aiclassroom-backend-$triple.exe") -Force

Write-Host "== 4/6 Frontend: dependencias, tests y build ==" -ForegroundColor Cyan
Push-Location (Join-Path $root "frontend")
try {
    npm ci
    if (-not $SkipTests) {
        npm run test
        if ($LASTEXITCODE -ne 0) { throw "Los tests del frontend han fallado." }
    }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "El build del frontend ha fallado." }
}
finally { Pop-Location }

Write-Host "== 5/6 Shell de escritorio (Tauri) ==" -ForegroundColor Cyan
Push-Location (Join-Path $root "src-tauri")
try {
    cargo build --$Configuration
    if ($LASTEXITCODE -ne 0) { throw "La compilación de Tauri ha fallado." }
}
finally { Pop-Location }

Write-Host "== 6/6 Carpeta portable ==" -ForegroundColor Cyan
if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Force -Path $staging | Out-Null

Copy-Item (Join-Path $root "src-tauri\target\$Configuration\ai-classroom-live.exe") `
          (Join-Path $staging "AI-Classroom-Live.exe")

$runtime = Join-Path $staging "runtime"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
Copy-Item (Join-Path $root "backend\dist\backend") $runtime -Recurse

# Pre-create the data folders so the first run never has to, which matters on a
# profile where the folder may be read-only in places (spec section 14).
foreach ($folder in @("config", "materials", "sessions", "metrics", "models")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $staging "data\$folder") | Out-Null
}

Copy-Item (Join-Path $root "docs\README-portable.txt") (Join-Path $staging "README.txt") -Force

$zip = Join-Path $release "AI-Classroom-Live-portable.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $staging -DestinationPath $zip

Write-Host ""
Write-Host "Carpeta portable: $staging" -ForegroundColor Green
Write-Host "Archivo comprimido: $zip" -ForegroundColor Green
Write-Host ""
Write-Host "Falta copiar el modelo de activación a data\models\oye_chat.onnx" -ForegroundColor Yellow
Write-Host "(se genera con scripts\train_wakeword.py)." -ForegroundColor Yellow
