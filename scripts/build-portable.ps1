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
    [switch]$SkipTests,

    # Risk R-7: an unsigned executable is what antivirus and SmartScreen react
    # to. Signing is optional because a code signing certificate costs money
    # and a school may not have one; without it the build still produces
    # SHA256SUMS.txt so the folder can at least be verified.
    [string]$CertificatePath,
    [string]$CertificatePassword,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
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
    $env:PATH = "$PWD\.venv\Scripts;$env:PATH"

    if (-not $SkipTests) {
        Write-Host "== 2/6 Backend: tests ==" -ForegroundColor Cyan
        & .\.venv\Scripts\python.exe -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw "Los tests del backend han fallado." }
    }

    Write-Host "== 3/6 Backend: empaquetado con PyInstaller ==" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m PyInstaller aiclassroom-backend.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller ha fallado." }

    & .\dist\backend\aiclassroom-backend.exe --selftest --require-audio --require-wakeword --require-training
    if ($LASTEXITCODE -ne 0) { throw "El backend empaquetado no supera el autotest." }
}
finally { Pop-Location }

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
    # custom-protocol embeds the interface; without it the window loads the
    # Vite dev server and shows ERR_CONNECTION_REFUSED (P-11).
    cargo build --$Configuration --features custom-protocol
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

# Fetched on this machine so the classroom PC never downloads anything (D-04).
Push-Location $root
try {
    python scripts\fetch_wakeword_runtime.py --output (Join-Path $staging "data\models")
    if ($LASTEXITCODE -ne 0) { throw "No se pudieron descargar los modelos de openWakeWord." }
}
finally { Pop-Location }

Copy-Item (Join-Path $root "docs\README-portable.txt") (Join-Path $staging "README.txt") -Force

# -- signing and checksums (risk R-7) --------------------------------------

$executables = Get-ChildItem $staging -Recurse -Include *.exe

if ($CertificatePath) {
    Write-Host "Firmando los ejecutables..." -ForegroundColor Cyan
    $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin" `
                              -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match "x64" } |
                Select-Object -First 1

    if (-not $signtool) { throw "No se encontró signtool.exe (Windows SDK)." }

    foreach ($executable in $executables) {
        & $signtool.FullName sign /fd SHA256 /f $CertificatePath `
            /p $CertificatePassword /tr $TimestampUrl /td SHA256 $executable.FullName
        if ($LASTEXITCODE -ne 0) { throw "No se pudo firmar $($executable.Name)." }
    }
    Write-Host "Firmados $($executables.Count) ejecutables." -ForegroundColor Green
}
else {
    Write-Host ""
    Write-Host "AVISO: los ejecutables no van firmados." -ForegroundColor Yellow
    Write-Host "Windows SmartScreen mostrara un aviso la primera vez, y algunos" -ForegroundColor Yellow
    Write-Host "antivirus pueden bloquear la carpeta. Ver docs/antivirus.md." -ForegroundColor Yellow
}

# A checksum file lets whoever receives the folder confirm it arrived intact,
# which is the next best thing to a signature.
$checksums = Join-Path $staging "SHA256SUMS.txt"
Get-ChildItem $staging -Recurse -File |
    Where-Object { $_.FullName -ne $checksums } |
    ForEach-Object {
        $relative = $_.FullName.Substring($staging.Length + 1)
        "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $relative
    } | Set-Content $checksums -Encoding ASCII

Write-Host "Sumas de verificacion en $checksums" -ForegroundColor Green

$zip = Join-Path $release "AI-Classroom-Live-portable.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $staging -DestinationPath $zip

Write-Host ""
Write-Host "Carpeta portable: $staging" -ForegroundColor Green
Write-Host "Archivo comprimido: $zip" -ForegroundColor Green
Write-Host ""
Write-Host "Falta copiar a data\models\ oye_chat.onnx y oye_chat.corpus.npz" -ForegroundColor Yellow
Write-Host "(se genera con scripts\train_wakeword.py)." -ForegroundColor Yellow
