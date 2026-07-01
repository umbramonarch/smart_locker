# File: run-native.ps1
# Description: Windows PowerShell equivalent of run-native.sh — starts Smart
#              Locker with the fake NFC reader and sim/data/ as the data source.
#              No QEMU, no PC/SC Smart Card service, no hardware.
# Project: smart_locker/sim/native
# Notes: Copy sim\.env.sim.example -> .env and fill in generated keys first.
#        Run from the repo root:  .\sim\native\run-native.ps1

$ErrorActionPreference = "Stop"

$repoRoot = (Get-Item "$PSScriptRoot\..\..").FullName
$envFile  = "$repoRoot\.env"

# ---------------------------------------------------------------------------
# Pre-flight: .env must exist and keys must be filled in
# ---------------------------------------------------------------------------
if (-not (Test-Path $envFile)) {
    Write-Error @"
.env not found at $envFile

Quick start:
  Copy-Item sim\.env.sim.example .env
  python -m scripts.generate_key        # paste two lines into .env
  python -m scripts.init_db
  python -m scripts.enroll_card --name "Sim User" --role admin --uid AABBCCDD
"@
    exit 1
}

function Check-Key([string]$varName) {
    $line = Get-Content $envFile | Where-Object { $_ -match "^${varName}=" } | Select-Object -First 1
    $val  = if ($line) { $line.Substring($line.IndexOf('=') + 1).Trim() } else { "" }
    if (-not $val -or $val -match "REPLACE_WITH") {
        Write-Error "${varName} is not set in .env -- run: python -m scripts.generate_key"
        exit 1
    }
}
Check-Key "SMART_LOCKER_ENC_KEY"
Check-Key "SMART_LOCKER_HMAC_KEY"

# ---------------------------------------------------------------------------
# Simulation env overrides (override whatever .env holds for these vars)
# ---------------------------------------------------------------------------
$env:SMART_LOCKER_FAKE_READER          = "1"
$env:SMART_LOCKER_FAKE_DEFAULT_UID     = if ($env:SMART_LOCKER_FAKE_DEFAULT_UID) { $env:SMART_LOCKER_FAKE_DEFAULT_UID } else { "AABBCCDD" }
$env:SMART_LOCKER_SOURCE_EXCEL_PATH    = "$repoRoot\sim\data\Messmittelliste.sample.xlsx"
$env:SMART_LOCKER_PHOTO_INPUT_PATH     = "$repoRoot\sim\data\photos"
$env:SMART_LOCKER_EXCEL_AUTO_EXPORT    = "0"

# ---------------------------------------------------------------------------
# Resolve venv Python
# ---------------------------------------------------------------------------
$python = "$repoRoot\venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error @"
venv not found at $repoRoot\venv.
Create it:
  python -m venv venv
  .\venv\Scripts\pip install -r requirements.txt
"@
    exit 1
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "==> Smart Locker -- native simulation run"
Write-Host "    repo    : $repoRoot"
Write-Host "    python  : $python"
Write-Host "    reader  : FAKE  (SMART_LOCKER_FAKE_READER=1)"
Write-Host "    uid     : $($env:SMART_LOCKER_FAKE_DEFAULT_UID)"
Write-Host "    source  : $($env:SMART_LOCKER_SOURCE_EXCEL_PATH)"
Write-Host "    photos  : $($env:SMART_LOCKER_PHOTO_INPUT_PATH)"
Write-Host ""
Write-Host "    Kiosk UI   -> http://localhost:8000"
Write-Host "    Inject tap -> POST /api/dev/tap  (or press F2 in the browser)"
Write-Host "    Import now -> python -m scripts.sync_source  (separate terminal)"
Write-Host ""

Set-Location $repoRoot
& $python -m smart_locker.app
