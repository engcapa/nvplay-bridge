#Requires -Version 7.0
# nvplay-bridge launcher for Windows 11. Brave only.
# On Linux / macOS use start-bridge.sh instead.

# ------------------------------ config ------------------------------
$Proxy      = "http://192.168.0.110:31028"   # "" = direct connection
$Python     = "python"                       # Python 3.12 interpreter
$CdpPort    = 9225
$BridgePort = 8083
$PoolSize   = 3
$BravePath  = ""                             # explicit brave.exe; "" = detect/download
$BraveDir   = Join-Path $PSScriptRoot ".brave"
$Playground = "https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground"
# --------------------------------------------------------------------

$ErrorActionPreference = "Stop"
if (-not $IsWindows) { throw "this launcher targets Windows; use start-bridge.sh on Linux/macOS" }
Set-Location $PSScriptRoot
$net = if ($Proxy) { @{ Proxy = $Proxy } } else { @{} }
$profileDir = Join-Path ([IO.Path]::GetTempPath()) "brave-cdp"
New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot "logs") | Out-Null

function Test-Cdp {
    try { Invoke-RestMethod "http://127.0.0.1:$CdpPort/json/version" -TimeoutSec 2 | Out-Null; $true } catch { $false }
}

function Find-Brave {
    $c = @($BravePath)
    $c += "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe"
    $c += "${env:ProgramFiles(x86)}\BraveSoftware\Brave-Browser\Application\brave.exe"
    $c += "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\Application\brave.exe"
    $c += Get-ChildItem $BraveDir -Recurse -File -ErrorAction SilentlyContinue |
          Where-Object { $_.Name -eq "brave.exe" } | Select-Object -ExpandProperty FullName
    $c | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}

function Install-Brave {
    $arch = if ("$([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture)" -eq "Arm64") { "arm64" } else { "x64" }
    $suffix = "win32-$arch.zip"

    $rel = Invoke-RestMethod "https://api.github.com/repos/brave/brave-browser/releases/latest" `
        -Headers @{ "User-Agent" = "nvplay-bridge" } @net
    $asset = $rel.assets |
        Where-Object { $_.name.EndsWith($suffix) -and $_.name -notmatch "symbols|origin" } |
        Select-Object -First 1
    if (-not $asset) { throw "no Brave build for $suffix" }

    Write-Host "downloading $($asset.name) ($([math]::Round($asset.size / 1MB)) MB) into $BraveDir"
    New-Item -ItemType Directory -Force $BraveDir | Out-Null
    $zip = Join-Path $BraveDir $asset.name
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip @net
    Expand-Archive $zip -DestinationPath $BraveDir -Force
    Remove-Item $zip
    Find-Brave
}

try { $pyver = (& $Python -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim() }
catch { throw "cannot run '$Python'; set `$Python at the top of this script" }
if ($pyver -ne "3.12") { throw "Python 3.12 required (found $pyver); set `$Python at the top of this script" }
Write-Host "==> Windows / Python $pyver"

Write-Host "==> dependencies"
$vendor = Join-Path $PSScriptRoot "vendor"
if (Test-Path -LiteralPath $vendor) {
    Write-Host "using offline bundle: vendor/"
} else {
    $pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "-r", (Join-Path $PSScriptRoot "requirements.txt"))
    if ($Proxy) { $pipArgs += @("--proxy", $Proxy) }
    & $Python @pipArgs
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}
$env:PYTHONPATH = if (Test-Path -LiteralPath $vendor) { "$PSScriptRoot;$vendor" } else { $PSScriptRoot }

Write-Host "==> browser (CDP $CdpPort)"
if (Test-Cdp) {
    Write-Host "reusing browser already listening on $CdpPort"
} else {
    $brave = Find-Brave
    if (-not $brave) {
        Write-Host "Brave not found, downloading a portable build"
        $brave = Install-Brave
    }
    Write-Host "starting $brave"
    Start-Process -FilePath $brave -ArgumentList @(
        "--user-data-dir=$profileDir",
        "--remote-debugging-port=$CdpPort",
        "--remote-allow-origins=*",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--window-size=1280,900", $Playground
    ) | Out-Null
    for ($i = 0; $i -lt 60 -and -not (Test-Cdp); $i++) { Start-Sleep -Milliseconds 500 }
    if (-not (Test-Cdp)) { throw "CDP did not start on port $CdpPort" }
}

Write-Host "==> bridge on http://127.0.0.1:$BridgePort  (Ctrl+C to stop)"
$env:UPSTREAM_KIND = "browser"
$env:BROWSER_CDP_PORT = "$CdpPort"
$env:BROWSER_POOL_SIZE = "$PoolSize"
$env:BROWSER_HEARTBEAT_SEC = "10"
$env:BROWSER_DEBUG_LOG = Join-Path (Join-Path $PSScriptRoot "logs") "bridge-debug.jsonl"

& $Python -m uvicorn nvplay_bridge:app --app-dir $PSScriptRoot --host 127.0.0.1 --port $BridgePort
