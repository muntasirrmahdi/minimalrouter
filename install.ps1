# MinimalRouter installer for Windows PowerShell.
#
# The router engine is cross-platform Python. This script mirrors install.sh:
# fetch the files, seed rows, build a dedicated Textual venv, and write a .cmd
# launcher. Pass -Curses to skip the dashboard venv.
#
# Usage (from a checkout):  powershell -ExecutionPolicy Bypass -File .\install.ps1
# Usage (remote):           irm https://raw.githubusercontent.com/muntasirrmahdi/minimalrouter/main/install.ps1 | iex
param([switch]$Curses)

$ErrorActionPreference = "Stop"
$Repo = "muntasirrmahdi/minimalrouter"
$Branch = "main"
$Raw = "https://raw.githubusercontent.com/$Repo/$Branch"

$HomeDir = $env:USERPROFILE
if (-not $HomeDir) { $HomeDir = $env:HOME }
if (-not $HomeDir) { throw "USERPROFILE/HOME is not set" }
$Dest = Join-Path $HomeDir ".minimalrouter"
$Bin = Join-Path $HomeDir ".local\bin"

function Say([string]$Text) { Write-Host $Text }

function Fetch([string]$Rel) {
    $local = Join-Path $PSScriptRoot $Rel
    $dst = Join-Path $Dest $Rel
    New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
    if (Test-Path $local) {
        Copy-Item -Force $local $dst
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri "$Raw/$Rel" -OutFile $dst
    }
}

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python is not on PATH. Install Python 3.8 or newer and re-run."
}
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,8) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.8 or newer is required." }

New-Item -ItemType Directory -Force -Path $Dest, $Bin | Out-Null

Fetch "minimalrouter.py"
Fetch "minimalrouter_dashboard.py"
Fetch "rows.example.json"
Fetch "oauth-providers.example.json"
Fetch "bridges/zcode-identity.json"
$registry = Join-Path $Dest "provider-registry.json"
if (-not (Test-Path $registry)) { Fetch "provider-registry.json" }

$rows = Join-Path $Dest "rows.json"
if (-not (Test-Path $rows)) {
    Copy-Item (Join-Path $Dest "rows.example.json") $rows
    Say "seeded $rows from the example rows"
}

$DashPy = $null
if (-not $Curses) {
    $venv = Join-Path $Dest ".venv"
    python -m venv $venv
    if ($LASTEXITCODE -eq 0) {
        $py = Join-Path $venv "Scripts\python.exe"
        & $py -m pip install --disable-pip-version-check textual 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $DashPy = $py
            Say "dashboard: Mission Control (Textual) ready"
        }
    }
    if (-not $DashPy) {
        Say "dashboard: Textual setup unavailable - using the built-in curses board"
    }
}

$launcher = Join-Path $Bin "minimalrouter.cmd"
if ($DashPy) {
    $line = "@echo off`r`n`"$DashPy`" `"$Dest\minimalrouter.py`" %*"
} else {
    $line = "@echo off`r`npython `"$Dest\minimalrouter.py`" %*"
}
Set-Content -Encoding ASCII -Path $launcher -Value $line

Say ""
Say "MinimalRouter installed"
Say "  program : $Dest\minimalrouter.py"
Say "  rows    : $rows"
Say "  command : $launcher"
Say ""
Say "next steps"
Say "  1. set the keys your rows name, for example: `$env:GROQ_API_KEY='...'"
Say "  2. run: $launcher"
Say "  3. press a to add a provider, t to test one, q to quit"
Say ""
Say "server only: $launcher serve"
if ($DashPy) { Say "dashboard: Mission Control board (Textual) - p themes, v density" }
else { Say "dashboard: built-in curses board (zero deps)" }
