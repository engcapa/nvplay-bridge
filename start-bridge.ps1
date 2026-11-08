<#
.SYNOPSIS
    Install dependencies, start a Chromium-based browser with CDP, and run the
    nvplay-bridge locally.

.DESCRIPTION
    One-command launcher for the bridge:

      1. optionally creates and uses a local .venv
      2. installs Python dependencies (pip install -r requirements.txt) unless an
         offline vendor/ bundle is present
      3. starts Brave (or Chrome/Chromium) with --remote-debugging-port and the
         Playground page open, if CDP is not already listening
      4. starts uvicorn on 127.0.0.1:<BridgePort> in the foreground

    Press Ctrl+C to stop the bridge. The browser window is left running.

.PARAMETER Python
    Python 3.12 interpreter used to create the venv and run the bridge.
    Defaults to "python" on PATH.

.PARAMETER BravePath
    Path to brave.exe (or chrome.exe / chromium.exe). When omitted the script
    searches the usual install locations and %BRAVE_PATH%.

.PARAMETER CdpPort
    Chrome DevTools Protocol port passed to the browser. Default 9225.

.PARAMETER BridgePort
    Port the FastAPI bridge listens on. Default 8083.

.PARAMETER PoolSize
    Number of Playground tabs (BROWSER_POOL_SIZE). Default 3.

.PARAMETER Venv
    Create and use .venv in the repository root.

.PARAMETER SkipPip
    Skip dependency installation (deps already present, or using vendor/).

.PARAMETER SkipBrave
    Do not start the browser (CDP is expected to be running already).

.EXAMPLE
    .\start-bridge.ps1 -Venv

.EXAMPLE
    .\start-bridge.ps1 -BravePath "C:\Portable\brave\brave.exe" -CdpPort 9222

.NOTES
    Set HTTPS_PROXY/HTTP_PROXY before running if pip needs a proxy to reach PyPI.
#>
#Requires -Version 7.0
[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$BravePath = "",
    [int]$CdpPort = 9225,
    [int]$BridgePort = 8083,
    [int]$PoolSize = 3,
    [switch]$Venv,
    [switch]$SkipPip,
    [switch]$SkipBrave
)

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot
Set-Location $RepoRoot

$PlaygroundUrl = "https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground"
$ProfileDir = Join-Path $env:TEMP "brave-cdp"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Test-Cdp {
    param([int]$Port)
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:$Port/json/version" -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Find-Browser {
    $candidates = @()
    if ($env:BRAVE_PATH) { $candidates += $env:BRAVE_PATH }
    $candidates += @(
        (Join-Path $env:ProgramFiles "BraveSoftware\Brave-Browser\Application\brave.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "BraveSoftware\Brave-Browser\Application\brave.exe"),
        (Join-Path $env:LOCALAPPDATA "BraveSoftware\Brave-Browser\Application\brave.exe"),
        (Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"),
        (Join-Path $env:LOCALAPPDATA "Google\Chrome\Application\chrome.exe")
    )
    foreach ($path in $candidates) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    return $null
}

# ---------------------------------------------------------------- 1. Python --
Write-Step "Python interpreter"
if ($Venv) {
    $venvDir = Join-Path $RepoRoot ".venv"
    $venvPython = Join-Path $venvDir "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Host "creating venv at $venvDir"
        & $Python -m venv $venvDir
        if ($LASTEXITCODE -ne 0) { throw "failed to create venv with '$Python'" }
    }
    $Python = $venvPython
}
$pyVersion = (& $Python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))").Trim()
Write-Host "$Python -> Python $pyVersion"
if ($pyVersion -ne "3.12") {
    Write-Warning "The pinned pydantic-core wheel targets CPython 3.12; Python $pyVersion may fail to import it."
}

# --------------------------------------------------------- 2. Dependencies --
if (-not $SkipPip) {
    Write-Step "Dependencies"
    $vendor = Join-Path $RepoRoot "vendor"
    $requirements = Join-Path $RepoRoot "requirements.txt"
    if (Test-Path -LiteralPath $vendor) {
        Write-Host "offline bundle found, using it instead of installing: $vendor"
    } else {
        Write-Host "python -m pip install -r requirements.txt"
        & $Python -m pip install --disable-pip-version-check -r $requirements
        if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    }
}

$env:PYTHONPATH = if (Test-Path -LiteralPath (Join-Path $RepoRoot "vendor")) {
    "$RepoRoot;$RepoRoot\vendor"
} else {
    $RepoRoot
}

# ---------------------------------------------------------------- 3. Brave --
if (-not $SkipBrave) {
    Write-Step "Browser (CDP $CdpPort)"
    if (Test-Cdp -Port $CdpPort) {
        Write-Host "CDP already listening on 127.0.0.1:$CdpPort, reusing it"
    } else {
        if (-not $BravePath) { $BravePath = Find-Browser }
        if (-not $BravePath) {
            throw "No Brave/Chrome found. Pass -BravePath <path-to-brave.exe> or set BRAVE_PATH."
        }
        Write-Host "starting $BravePath"
        Start-Process -FilePath $BravePath -ArgumentList @(
            "--user-data-dir=$ProfileDir",
            "--remote-debugging-port=$CdpPort",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--window-size=1280,900",
            $PlaygroundUrl
        ) | Out-Null

        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
            if (Test-Cdp -Port $CdpPort) { break }
        }
        if (-not (Test-Cdp -Port $CdpPort)) {
            throw "CDP did not come up on port $CdpPort. Close every browser process using $ProfileDir and retry."
        }
        Write-Host "CDP is up"
        Write-Host "warm-up: the first request needs a profile that already passed the Playground hCaptcha"
    }
}

# ---------------------------------------------------------------- 4. Bridge --
Write-Step "Bridge"
$env:UPSTREAM_KIND = "browser"
$env:BROWSER_CDP_PORT = "$CdpPort"
$env:BROWSER_POOL_SIZE = "$PoolSize"
$env:BROWSER_HEARTBEAT_SEC = "10"
$env:BROWSER_DEBUG_LOG = Join-Path $RepoRoot "logs\bridge-debug.jsonl"

New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "logs") | Out-Null

Write-Host "listening on http://127.0.0.1:$BridgePort  (health: /health)"
Write-Host "Claude Code: ANTHROPIC_BASE_URL=http://127.0.0.1:$BridgePort"
Write-Host "stop with Ctrl+C"
Write-Host ""

& $Python -m uvicorn nvplay_bridge:app `
    --app-dir $RepoRoot `
    --host 127.0.0.1 `
    --port $BridgePort
