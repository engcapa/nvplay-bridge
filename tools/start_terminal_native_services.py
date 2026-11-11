"""Start independent Brave/bridge benchmark services with an audited 128K protocol."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request

from native_eval_support import write_json
from eval_terminal_native import process_identity


MODULES = ("nvplay_bridge.py", "browser_upstream.py", "browser_events.py", "stream_support.py",
           "responses_api.py", "responses_support.py")
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def launch(args, base, name, env=None):
    with (base / f"{name}.log").open("ab") as log:
        process = subprocess.Popen(list(map(str, args)), stdout=log, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
    identity = process_identity(process.pid)
    if identity is None:
        raise RuntimeError(f"{name} exited before its process identity was recorded; inspect its log")
    write_json(base / f"{name}-process.json", {**identity, "args": list(map(str, args)),
                                             "started_at": time.time()})
    return process


def await_json(url, child):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f"service exited with {child.returncode}; inspect its log")
        try:
            with LOCAL_HTTP.open(url, timeout=2) as response:
                return json.load(response)
        except (OSError, ValueError):
            time.sleep(0.5)
    raise TimeoutError(f"service did not become ready: {url}; process retained for inspection")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--brave", type=Path, required=True)
    parser.add_argument("--proxy", default="http://127.0.0.1:31128")
    parser.add_argument("--cdp-port", type=int, default=9225)
    parser.add_argument("--bridge-port", type=int, default=8083)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    os.chdir(repo)
    base = args.base.resolve()
    base.mkdir(parents=True, exist_ok=True)
    for port in (args.cdp_port, args.bridge_port):
        with socket.socket() as connection:
            if connection.connect_ex(("127.0.0.1", port)) == 0:
                parser.error(f"port {port} is occupied; do not replace a potentially active service")
    brave = launch([args.brave.resolve(), "--no-sandbox", f"--user-data-dir={base}/brave-profile",
                    f"--remote-debugging-port={args.cdp_port}", "--remote-allow-origins=*",
                    f"--proxy-server={args.proxy}", "--no-first-run", "--no-default-browser-check",
                    "--disable-extensions", "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows", "--window-size=1280,900",
                    "https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground"], base, "brave")
    await_json(f"http://127.0.0.1:{args.cdp_port}/json/version", brave)
    settings = {"UPSTREAM_KIND": "browser", "UPSTREAM_MODEL": "moonshotai/kimi-k3.1-pro",
                "BROWSER_CDP_PORT": str(args.cdp_port), "BROWSER_POOL_SIZE": "5", "BROWSER_TIMEOUT": "28800",
                "BROWSER_FIRST_BYTE_TIMEOUT": "3600", "BROWSER_IDLE_TIMEOUT": "3600",
                "BROWSER_MAX_TOKENS": "131072", "BROWSER_FIXED_MAX_TOKENS": "131072",
                "BROWSER_HTTP_5XX_RETRIES": "2", "BROWSER_DEBUG_LOG": str(base / "bridge-debug.jsonl")}
    identity = {"started_at": time.time(),
                "source_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "source_module_sha256": {name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in MODULES},
                "configuration": settings, "fixed_max_output_tokens": 131072, "request_timeout_seconds": 28800,
                "first_byte_timeout_seconds": 3600, "idle_timeout_seconds": 3600,
                "cdp_port": args.cdp_port, "bridge_port": args.bridge_port}
    bridge = launch(["python3", "-m", "uvicorn", "nvplay_bridge:app", "--host", "127.0.0.1",
                     "--port", str(args.bridge_port), "--timeout-keep-alive", "75"], base, "bridge-server",
                    env={**os.environ, **settings, "NO_PROXY": "127.0.0.1,localhost"})
    identity.update(process_identity(bridge.pid) or {"pid": bridge.pid})
    identity["health_at_start"] = await_json(f"http://127.0.0.1:{args.bridge_port}/health", bridge)
    write_json(base / "bridge-runtime.json", identity)
    launch(["python3", "-u", "tools/monitor_bridge.py", "--log", base / "bridge-debug.jsonl", "--duration", "0",
            "--interval", "5", "--health-url", f"http://127.0.0.1:{args.bridge_port}/health"], base, "bridge-monitor")
    print(json.dumps({"brave_pid": brave.pid, "bridge_pid": bridge.pid, "base": str(base)}))


if __name__ == "__main__":
    main()
