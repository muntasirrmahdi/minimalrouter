#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $HOME '.minimalrouter'),
    [string]$BinDirectory = (Join-Path $HOME '.local\bin'),
    [switch]$SkipDependencies
)

$ErrorActionPreference = 'Stop'
$Repo = 'muntasirrmahdi/minimalrouter'
$Branch = 'main'
$Raw = "https://raw.githubusercontent.com/$Repo/$Branch"

function Write-Step([string]$Message) { Write-Host $Message }

function Find-Python {
    foreach ($candidate in @('py', 'python', 'python3')) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $arguments = if ($candidate -eq 'py') { @('-3', '-c', 'import sys; print(sys.version_info[:2] >= (3, 10))') } else { @('-c', 'import sys; print(sys.version_info[:2] >= (3, 10))') }
        try {
            $result = & $command.Source @arguments 2>$null
            if ("$result".Trim() -eq 'True') { return ,@($command.Source, $candidate) }
        } catch { }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host 'error: Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/windows/ (tick "Add python.exe to PATH") and re-run.' -ForegroundColor Red
    exit 1
}
$pythonExe = $python[0]
$pythonKind = $python[1]

New-Item -ItemType Directory -Force -Path $Destination, $BinDirectory | Out-Null

function Get-MinimalRouterFile([string]$Name) {
    $local = Join-Path (Get-Location) $Name
    $target = Join-Path $Destination $Name
    if (Test-Path $local) {
        Copy-Item $local $target -Force
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri "$Raw/$Name" -OutFile $target
    }
}

Get-MinimalRouterFile 'minimalrouter.py'
Get-MinimalRouterFile 'rows.example.json'
Get-MinimalRouterFile 'oauth-providers.example.json'
if (-not (Test-Path (Join-Path $Destination 'provider-registry.json'))) {
    Get-MinimalRouterFile 'provider-registry.json'
}
if (-not (Test-Path (Join-Path $Destination 'rows.json'))) {
    Copy-Item (Join-Path $Destination 'rows.example.json') (Join-Path $Destination 'rows.json')
    Write-Step "seeded $Destination\rows.json from the example rows"
}

# Credentials stay inside the user profile; Windows has no chmod, so rely on
# the default ACL of %USERPROFILE% (owner-only) rather than file modes.
$runtime = Join-Path $Destination 'python-runtime'
if (-not $SkipDependencies) {
    New-Item -ItemType Directory -Force -Path $runtime | Out-Null
    Write-Step "installing dashboard and OpenCode dependencies into $runtime ..."
    $pipArguments = if ($pythonKind -eq 'py') { @('-3', '-m', 'pip', 'install', '--upgrade', '--target', $runtime, 'windows-curses', 'curl_cffi') } else { @('-m', 'pip', 'install', '--upgrade', '--target', $runtime, 'windows-curses', 'curl_cffi') }
    & $pythonExe @pipArguments
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'warning: dependency install failed. The server still runs; the dashboard needs windows-curses and OpenCode Free needs curl_cffi.' -ForegroundColor Yellow
        Write-Host "  retry: $pythonExe -m pip install --target `"$runtime`" windows-curses curl_cffi"
    }
}

$script = Join-Path $Destination 'minimalrouter.py'
$launcher = Join-Path $BinDirectory 'minimalrouter.cmd'
$launcherBody = "@echo off`r`nset PYTHONUTF8=1`r`n`"$pythonExe`" `"$script`" %*`r`n"
Set-Content -Path $launcher -Value $launcherBody -Encoding ASCII

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $userPath) { $userPath = '' }
$entries = $userPath -split ';' | Where-Object { $_ }
if ($entries -notcontains $BinDirectory) {
    $newPath = (@($entries) + $BinDirectory) -join ';'
    if ($newPath.Length -ge 1900) {
        Write-Host "warning: your user PATH is close to the Windows limit; add this folder yourself if needed: $BinDirectory" -ForegroundColor Yellow
    } else {
        [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
        Write-Step "added $BinDirectory to your user PATH (open a new terminal to use it)"
    }
}
$env:Path = "$BinDirectory;$env:Path"

Write-Step ''
Write-Step 'MinimalRouter installed'
Write-Step "  program : $script"
Write-Step "  rows    : $(Join-Path $Destination 'rows.json')"
Write-Step "  command : $launcher"
Write-Step ''
Write-Step 'next steps'
Write-Step '  1. open a new terminal (PATH change)'
Write-Step '  2. run: minimalrouter'
Write-Step '  3. press A to add a provider, ENTER to open its models, Q to quit'
Write-Step ''
Write-Step 'server only: minimalrouter serve'
