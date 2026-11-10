"""Unscored real-CC/CDP fault probe. Synthetic local provider; no NVIDIA calls."""
import argparse
import asyncio
import hashlib
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "tools")]
import browser_upstream as browser
import nvplay_bridge as bridge
import uvicorn
from native_eval_support import cc_result, run_logged, write_json
from tools.monitor_cdp_network import observe

PAGE = b'''<!doctype html><meta charset=utf-8>
<textarea aria-label="chat prompt"></textarea><button aria-label="Send">Send</button>
<script>window.hcaptcha={execute(){}};
document.querySelector('button').onclick=()=>fetch('/v2/predict/models/cc-fault-fixture',
{method:'POST',body:'{}'}).then(r=>r.text()).catch(()=>{});</script>'''
MARKER = "CC_BRIDGE_CONTROLLED_STREAM_OK"
active = {}
fixture_events = []
lock = threading.Lock()


def frame(delta, finish=None):
    value = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return ("data: " + json.dumps(value) + "\n\n").encode()


class Fixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        with lock:
            active["requests"] += 1
            broken = active["requests"] == 1
            tool = {"tool_calls": [{"index": 0, "id": "reused-provider-tool:0", "type": "function",
                     "function": {"name": "Bash", "arguments": json.dumps({
                         "command": active["incomplete_command"] if broken else active["command"],
                         "description": "Controlled local counter"})}}]}
            has_result = any(message.get("role") == "tool" for message in request.get("messages", []))
            if broken:
                if active["mode"] == "complete-tool-args":
                    delta = tool
                elif active["mode"] in ("thinking-only", "thinking-and-text"):
                    delta = {"reasoning_content": "Controlled partial thinking. " * 24,
                             "content": "PARTIAL_CONTROLLED_TEXT"}
                    if active["mode"] == "thinking-only":
                        del delta["content"]
                else:
                    delta = {"content": "PARTIAL_CONTROLLED_TEXT"}
                body = frame(delta)
                kind = "incomplete_" + active["mode"]
            elif has_result:
                body = frame({"content": MARKER}, "stop") + b"data: [DONE]\n\n"
                kind = "final_text"
            else:
                body = frame(tool, "tool_calls") + b"data: [DONE]\n\n"
                kind = "complete_tool"
            fixture_events.append({"ts": time.time(), "case": active["name"],
                                   "request": active["requests"], "kind": kind,
                                   "max_tokens": request.get("max_tokens")})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body) + 100000 if broken else len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        if broken:
            time.sleep(1)
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)

    def log_message(self, *args):
        pass


def count(path):
    return int(path.read_text()) if path.exists() else 0


def counter_command(path):
    code = ("from pathlib import Path; p=Path(" + repr(str(path))
            + "); p.write_text(str(int(p.read_text())+1 if p.exists() else 1)); print('COUNTER_WRITTEN')")
    return "python3 -I -c " + shlex.quote(code)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cdp-port", type=int, default=9225)
    parser.add_argument("--pinned-cli", type=Path, help="Optional official Node.js CC cli.js to compare")
    parser.add_argument("--fixed-max-tokens", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.fixed_max_tokens <= bridge.BROWSER_MAX_TOKENS:
        parser.error("fixed output cap must be between zero and BROWSER_MAX_TOKENS")
    HERE = args.output.resolve()
    HERE.mkdir(parents=True, exist_ok=True)
    if (HERE / "result.json").exists() or any(HERE.glob("*/initial.jsonl")):
        raise SystemExit("Existing evidence must not be overwritten.")
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    serving = threading.Thread(target=fixture.serve_forever, daemon=True)
    serving.start()
    hint = "cc-stream-fault=" + uuid.uuid4().hex
    local_url = f"http://127.0.0.1:{fixture.server_port}/?{hint}"
    cdp = f"http://127.0.0.1:{args.cdp_port}"
    target = tab = api = api_thread = observer = None
    stop = threading.Event()
    connected = threading.Event()
    network = []
    rows = []

    def emit(event):
        network.append({"ts": time.time(), **event})
        if event["event"] == "observer_connected":
            connected.set()

    try:
        new = browser.urllib.request.Request(cdp + "/json/new?" + urllib.parse.quote(local_url, safe=""), method="PUT")
        with browser.LOCAL_HTTP.open(new, timeout=5) as response:
            target = json.load(response)
        observer = threading.Thread(target=observe, args=(target, stop, emit), daemon=True)
        observer.start()
        assert connected.wait(5)
        tab = browser.BrowserUpstream(port=args.cdp_port, target_hint=hint)
        tab._connect()
        tab._wait_ready(threading.Event(), time.monotonic() + 10)
        bridge.UPSTREAM_KIND = "browser"
        bridge._browser = tab
        bridge.BROWSER_TIMEOUT = 60
        bridge.BROWSER_FIXED_MAX_TOKENS = args.fixed_max_tokens
        bridge.BROWSER_HEARTBEAT_SEC = 0.2
        bridge.BROWSER_DEBUG_LOG = str(HERE / "bridge-debug.jsonl")
        listen = socket.socket()
        listen.bind(("127.0.0.1", 0))
        listen.listen(128)
        port = listen.getsockname()[1]
        api = uvicorn.Server(uvicorn.Config(bridge.app, log_level="error"))
        api_thread = threading.Thread(target=lambda: asyncio.run(api.serve(sockets=[listen])), daemon=True)
        api_thread.start()
        for _ in range(100):
            if api.started:
                break
            time.sleep(.05)
        assert api.started
        flavors = {"latest-native": [shutil.which("claude")]}
        if args.pinned_cli:
            flavors = {"pinned-node": [shutil.which("node"), str(args.pinned_cli.resolve())], **flavors}
        versions = {name: subprocess.check_output(command + ["--version"], text=True).strip()
                    for name, command in flavors.items()}
        write_json(HERE / "identity.json", {"cli_versions": versions,
            "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "inference_sha256": {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
                for name in ("nvplay_bridge.py", "browser_upstream.py", "browser_events.py", "stream_support.py")},
            "fixed_max_tokens": args.fixed_max_tokens, "nvidia_requests": 0})
        os.chdir(HERE)
        for flavor, cli in flavors.items():
            for mode in ("text", "thinking-only", "thinking-and-text", "complete-tool-args"):
                name = flavor + "--" + mode
                out = HERE / name
                out.mkdir()
                counter = out / "counter.txt"
                incomplete_counter = out / "incomplete-counter.txt"
                with lock:
                    active.update(name=name, mode=mode, requests=0,
                                  command=counter_command(counter),
                                  incomplete_command=counter_command(incomplete_counter))
                settings = {"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                    "ANTHROPIC_AUTH_TOKEN": "local-fault-fixture-token", "ANTHROPIC_MODEL": "kimi-k3.1-pro",
                    "CLAUDE_CODE_ALLOW_OTHER_MODELS": "1", "CLAUDE_CONFIG_DIR": str(out / "config"),
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000", "API_TIMEOUT_MS": "180000",
                    "CLAUDE_CODE_EXTRA_BODY": json.dumps({"max_tokens": 131072, "output_config": {"effort": "low"}}),
                    "DISABLE_AUTOUPDATER": "1", "DISABLE_AUTO_COMPACT": "1"}, "enabledPlugins": {}}
                write_json(out / "settings.json", settings)
                write_json(out / "mcp.json", {"mcpServers": {}})
                shared = ["--output-format", "stream-json", "--verbose", "--settings", str(out / "settings.json"),
                    "--setting-sources", "", "--strict-mcp-config", "--mcp-config", str(out / "mcp.json"),
                    "--permission-mode", "bypassPermissions", "--disable-slash-commands", "--tools", "Bash",
                    "--model", "kimi-k3.1-pro", "--effort", "low", "--max-turns", "6"]
                session = str(uuid.uuid4())
                initial = run_logged(cli + ["-p", "This is a controlled local transport fixture. Execute the supplied counter tool once, then finish."]
                    + shared + ["--session-id", session], out / "initial.jsonl", 300, out / "initial-state.json")
                first_client = cc_result(out / "initial.jsonl")
                first_count = count(counter)
                first_incomplete_count = count(incomplete_counter)
                events = [event for event in fixture_events if event["case"] == name]
                first_completes = sum(event["kind"] == "complete_tool" for event in events)
                resumed = second_client = None
                if initial["exit_code"] != 0 or (first_client or {}).get("is_error"):
                    resumed = run_logged(cli + ["-p", "Continue the controlled fixture after the transport interruption."]
                        + shared + ["--resume", session], out / "resume.jsonl", 300, out / "resume-state.json")
                    second_client = cc_result(out / "resume.jsonl")
                final_client = second_client if resumed else first_client
                final_process = resumed or initial
                events = [event for event in fixture_events if event["case"] == name]
                row = {"case": name, "initial": initial, "initial_counter": first_count,
                    "initial_complete_tool_responses": first_completes,
                    "initial_incomplete_tool_executed": first_incomplete_count > 0 or first_count > first_completes,
                    "initial_cc_error": (first_client or {}).get("is_error"),
                    "automatic_recovery": initial["exit_code"] == 0 and not (first_client or {}).get("is_error", True),
                    "resume": resumed, "final_counter": count(counter),
                    "incomplete_response_counter": count(incomplete_counter), "fixture_events": events,
                    "success": first_incomplete_count == 0 and count(incomplete_counter) == 0
                        and first_count <= first_completes and count(counter) == 1
                        and final_process["exit_code"] == 0 and not final_process["timed_out"]
                        and bool(final_client) and not final_client["is_error"]
                        and MARKER in (final_client.get("result") or "")}
                row["observed_provider_limits"] = sorted({event["max_tokens"] for event in events})
                row["fixed_max_tokens"] = args.fixed_max_tokens
                if args.fixed_max_tokens:
                    row["success"] = row["success"] and row["observed_provider_limits"] == [args.fixed_max_tokens]
                rows.append(row)
                write_json(out / "result.json", row)
                print(json.dumps(row), flush=True)
    finally:
        stop.set()
        if observer:
            observer.join(3)
        if api:
            api.should_exit = True
        if api_thread:
            api_thread.join(5)
        if tab:
            tab.close()
        if target:
            with browser.LOCAL_HTTP.open(cdp + "/json/close/" + target["id"], timeout=5):
                pass
        fixture.shutdown()
        fixture.server_close()
        serving.join(2)
        write_json(HERE / "cdp-network.json", network)
        write_json(HERE / "fixture-events.json", fixture_events)
    write_json(HERE / "result.json", {"success": len(rows) == 4 * len(flavors) and all(row["success"] for row in rows),
        "results": rows, "nvidia_requests": 0, "stage": "unscored", "provider": "synthetic local fixture",
        "limits": "Tests client/bridge protocol behavior; not a reproduction of NVIDIA root cause or a model quality evaluation."})
    if not all(row["success"] for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
