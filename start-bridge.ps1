#Requires -Version 7.0
# nvplay-bridge launcher: install deps, get a browser with CDP, run the bridge.

# ------------------------------ config ------------------------------
$Proxy      = "http://192.168.0.110:31028"   # "" = direct connection
$Python     = "python"                       # Python 3.12 interpreter
$CdpPort    = 9225
$BridgePort = 8083
$PoolSize   = 3
$BravePath  = ""                             # explicit brave binary; "" = detect/download
$BrowserDir = Join-Path $PSScriptRoot ".browser"
$Playground = "https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground"
# --------------------------------------------------------------------

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$net = if ($Proxy) { @{ Proxy = $Proxy } } else { @{} }
New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot "logs") | Out-Null

try { $pyver = (& $Python -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim() }
catch { throw "cannot run '$Python'; set `$Python at the top of this script" }
if ($pyver -ne "3.12") { throw "Python 3.12 required (found $pyver); set `$Python at the top of this script" }
Write-Host "using Python $pyver"

function Test-Cdp {
    try { Invoke-RestMethod "http://127.0.0.1:$CdpPort/json/version" -TimeoutSec 2 @net | Out-Null; $true } catch { $false }
}

function Find-Brave {
    $c = @($BravePath)
    if ($IsWindows) {
        $c += "$env:ProgramFiles\BraveSoftware\Brave-Browser\Application\brave.exe"
        $c += "${env:ProgramFiles(x86)}\BraveSoftware\Brave-Browser\Application\brave.exe"
        $c += "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\Application\brave.exe"
    } elseif ($IsMacOS) {
        $c += "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    } else {
        $c += "/usr/bin/brave-browser", "/opt/brave.com/brave/brave"
    }
    $c += Get-ChildItem $BrowserDir -Recurse -File -ErrorAction SilentlyContinue |
          Where-Object { $_.Name -in "brave.exe", "brave", "Brave Browser" } |
          Select-Object -ExpandProperty FullName
    $c | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}

function Install-BravePortable {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -match 'ARM64|arm64') { 'arm64' } else { 'x64' }
    $suffix = if ($IsWindows) { "win32-$arch.zip" } elseif ($IsMacOS) { "darwin-$arch.zip" } else { "linux-$(if ($arch -eq 'arm64') { 'arm64' } else { 'amd64' }).zip" }
    $rel = Invoke-RestMethod "https://api.github.com/repos/brave/brave-browser/releases/latest" `
        -Headers @{ "User-Agent" = "nvplay-bridge" } @net
    $asset = $rel.assets |
        Where-Object { $_.name.EndsWith($suffix) -and $_.name -notmatch "symbols|origin" } |
        Select-Object -First 1
    if (-not $asset) { throw "no Brave portable build for $suffix" }

    Write-Host "downloading $($asset.name) ($([math]::Round($asset.size / 1MB)) MB)"
    New-Item -ItemType Directory -Force $BrowserDir | Out-Null
    $zip = Join-Path $BrowserDir $asset.name
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip @net
    Expand-Archive $zip -DestinationPath $BrowserDir -Force
    Remove-Item $zip
    $bin = Get-ChildItem $BrowserDir -Recurse -File |
           Where-Object { $_.Name -in "brave.exe", "brave", "Brave Browser" } | Select-Object -First 1
    if (-not $bin) { throw "brave binary not found after extracting $($asset.name)" }
    if (-not $IsWindows) { & chmod +x $bin.FullName }
    $bin.FullName
}

Write-Host "==> dependencies"
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot "vendor")) {
    Write-Host "using offline bundle: vendor/"
} else {
    $pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "-r", (Join-Path $PSScriptRoot "requirements.txt"))
    if ($Proxy) { $pipArgs += @("--proxy", $Proxy) }
    & $Python @pipArgs
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}
$vendor = Join-Path $PSScriptRoot "vendor"
$env:PYTHONPATH = if (Test-Path -LiteralPath $vendor) {
    "$PSScriptRoot$([IO.Path]::PathSeparator)$vendor"
} else {
    $PSScriptRoot
}

Write-Host "==> browser (CDP $CdpPort)"
if (Test-Cdp) {
    Write-Host "reusing browser already listening on $CdpPort"
} else {
    $brave = Find-Brave
    if (-not $brave) {
        Write-Host "Brave not found, fetching a portable build"
        $brave = Install-BravePortable
    }
    Write-Host "starting $brave"
    Start-Process -FilePath $brave -ArgumentList @(
        "--user-data-dir=$(Join-Path $env:TEMP 'brave-cdp')",
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
