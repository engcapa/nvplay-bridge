#!/usr/bin/env bash
# nvplay-bridge launcher for Linux and macOS. Brave only.
# Requires bash 3.2+ (the stock macOS /bin/bash works).
set -euo pipefail

# ------------------------------ config ------------------------------
PROXY="http://192.168.0.110:31028"   # "" = direct connection
PYTHON="python3"                     # Python 3.12 interpreter
CDP_PORT=9225
BRIDGE_PORT=8083
POOL_SIZE=3
BRAVE_PATH=""                        # explicit brave binary; "" = detect/download
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRAVE_DIR="$REPO_DIR/.brave"
PLAYGROUND="https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground"
# --------------------------------------------------------------------

cd "$REPO_DIR"
mkdir -p logs
PROFILE_DIR="${TMPDIR:-/tmp}/brave-cdp"

case "$(uname -s)" in
    Linux) OS=linux ;;
    Darwin) OS=macos ;;
    *) echo "unsupported OS: $(uname -s) (use start-bridge.ps1 on Windows)" >&2; exit 1 ;;
esac

case "$(uname -m)" in
    x86_64 | amd64) ARCH=x64 ;;
    arm64 | aarch64) ARCH=arm64 ;;
    *) echo "unsupported CPU architecture: $(uname -m)" >&2; exit 1 ;;
esac

if [ "$OS" = macos ]; then
    SUFFIX="darwin-$ARCH.zip"
elif [ "$ARCH" = arm64 ]; then
    SUFFIX="linux-arm64.zip"
else
    SUFFIX="linux-amd64.zip"
fi

fetch() { if [ -n "$PROXY" ]; then curl -fsSL -x "$PROXY" "$@"; else curl -fsSL "$@"; fi; }

download() { if [ -n "$PROXY" ]; then curl -fL -x "$PROXY" -o "$2" "$1"; else curl -fL -o "$2" "$1"; fi; }

cdp_up() { curl -fsS --max-time 2 --noproxy '*' "http://127.0.0.1:$CDP_PORT/json/version" >/dev/null 2>&1; }

find_brave() {
    local c=() p
    [ -n "$BRAVE_PATH" ] && c+=("$BRAVE_PATH")
    if [ "$OS" = macos ]; then
        c+=("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
        c+=("$HOME/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
    else
        c+=(/usr/bin/brave-browser /usr/bin/brave /opt/brave.com/brave/brave)
    fi
    if [ -d "$BRAVE_DIR" ]; then
        while IFS= read -r p; do c+=("$p"); done < <(find "$BRAVE_DIR" -type f \( -name brave -o -name "Brave Browser" \) 2>/dev/null)
    fi
    for p in "${c[@]}"; do
        if [ -n "$p" ] && [ -x "$p" ]; then
            printf '%s\n' "$p"
            return 0
        fi
    done
    return 1
}

install_brave() {
    local api tmp name url zip
    api="https://api.github.com/repos/brave/brave-browser/releases/latest"
    tmp="$(mktemp)"
    fetch -H "User-Agent: nvplay-bridge" -o "$tmp" "$api"

    IFS=$'\t' read -r name url < <("$PYTHON" - "$tmp" "$SUFFIX" <<'PY'
import json, sys
assets = json.load(open(sys.argv[1]))["assets"]
suffix = sys.argv[2]
match = next((a for a in assets
              if a["name"].endswith(suffix) and "symbols" not in a["name"] and "origin" not in a["name"]), None)
if match:
    print(match["name"] + "\t" + match["browser_download_url"])
PY
    ) || true
    rm -f "$tmp"
    if [ -z "${name:-}" ]; then
        echo "no Brave build for $SUFFIX" >&2
        return 1
    fi

    echo "downloading $name into $BRAVE_DIR" >&2
    mkdir -p "$BRAVE_DIR"
    zip="$BRAVE_DIR/$name"
    download "$url" "$zip"
    "$PYTHON" -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$zip" "$BRAVE_DIR"
    rm -f "$zip"
    chmod -R +x "$BRAVE_DIR"
    find_brave
}

ver="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" || { echo "cannot run '$PYTHON'" >&2; exit 1; }
if [ "$ver" != "3.12" ]; then
    echo "Python 3.12 required (found $ver); set PYTHON at the top of this script" >&2
    exit 1
fi
echo "==> $OS / $ARCH / Python $ver"

echo "==> dependencies"
if [ -d "$REPO_DIR/vendor" ]; then
    echo "using offline bundle: vendor/"
    export PYTHONPATH="$REPO_DIR:$REPO_DIR/vendor"
else
    if [ -n "$PROXY" ]; then
        "$PYTHON" -m pip install --disable-pip-version-check --proxy "$PROXY" -r "$REPO_DIR/requirements.txt"
    else
        "$PYTHON" -m pip install --disable-pip-version-check -r "$REPO_DIR/requirements.txt"
    fi
    export PYTHONPATH="$REPO_DIR"
fi

echo "==> browser (CDP $CDP_PORT)"
if cdp_up; then
    echo "reusing browser already listening on $CDP_PORT"
else
    brave="$(find_brave || true)"
    if [ -z "$brave" ]; then
        echo "Brave not found, downloading a portable build" >&2
        brave="$(install_brave)"
    fi
    echo "starting $brave"
    args=(--user-data-dir="$PROFILE_DIR" --remote-debugging-port="$CDP_PORT" --remote-allow-origins='*'
          --no-first-run --no-default-browser-check --disable-extensions --window-size=1280,900 "$PLAYGROUND")
    if [ "$(id -u)" = "0" ]; then args=(--no-sandbox "${args[@]}"); fi
    "$brave" "${args[@]}" >/dev/null 2>&1 &
    for _ in $(seq 1 60); do cdp_up && break; sleep 0.5; done
    if ! cdp_up; then echo "CDP did not start on port $CDP_PORT" >&2; exit 1; fi
fi

echo "==> bridge on http://127.0.0.1:$BRIDGE_PORT  (Ctrl+C to stop)"
export UPSTREAM_KIND=browser
export BROWSER_CDP_PORT="$CDP_PORT"
export BROWSER_POOL_SIZE="$POOL_SIZE"
export BROWSER_HEARTBEAT_SEC=10
export BROWSER_DEBUG_LOG="$REPO_DIR/logs/bridge-debug.jsonl"

exec "$PYTHON" -m uvicorn nvplay_bridge:app --app-dir "$REPO_DIR" --host 127.0.0.1 --port "$BRIDGE_PORT"
