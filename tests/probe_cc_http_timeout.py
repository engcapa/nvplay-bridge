"""Opt-in real-CC fallback/transport-timeout probe against a local Messages fixture.

No benchmark input, model invocation, or NVIDIA endpoint is used. The first
stream fails, and the first non-streaming response delays headers or its body.
Later requests complete immediately.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import threading
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from native_eval_support import kill_uid
from native_path_cc import uid_processes

MARKER = "CONTROLLED_DELAYED_HEADERS_OK"


def sse(kind, value):
    return f"event: {kind}\ndata: {json.dumps(value)}\n\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pinned-cli", type=Path, required=True)
    parser.add_argument("--delay-seconds", type=float, default=310)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--include-native", action="store_true")
    parser.add_argument("--adapter-only", action="store_true")
    parser.add_argument("--native-root", type=Path, help="Isolated native PATH runtime; runs only this case")
    parser.add_argument("--uid", type=int)
    parser.add_argument("--proot", default="proot")
    parser.add_argument("--delay-phase", choices=("headers", "body"), default="headers")
    parser.add_argument("--settings-env", type=Path, help="Copy real request environment, replacing fixture routing/auth")
    parser.add_argument("--disable-native-idle-timeout", action="store_true")
    parser.add_argument("--cases", nargs="+", choices=(
        "node-settings", "node-process-env", "node-adapter", "native-settings"))
    args = parser.parse_args()
    if not args.native_root and (args.delay_phase != "headers" or args.settings_env
                                 or args.disable_native_idle_timeout):
        parser.error("body/environment options require --native-root")
    if args.delay_seconds <= 0:
        parser.error("--delay-seconds must be positive")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    pinned = [shutil.which("node"), str(args.pinned_cli.resolve())]
    cases = [("node-settings", pinned, False, False), ("node-process-env", pinned, True, False)]
    if args.adapter:
        cases.append(("node-adapter", pinned, True, True))
    if args.include_native:
        cases.append(("native-settings", [shutil.which("claude")], False, False))
    if args.adapter_only:
        if not args.adapter:
            parser.error("--adapter-only requires --adapter")
        cases = [case for case in cases if case[3]]
    if args.cases:
        cases = [case for case in cases if case[0] in args.cases]
        if len(cases) != len(set(args.cases)):
            parser.error("requested case requires --adapter or --include-native")
    if args.native_root:
        if os.geteuid() != 0 or args.uid is None or not 20000 <= args.uid <= 29999:
            parser.error("native PRoot probe needs sudo and a dedicated evaluation UID")
        if uid_processes(args.uid):
            parser.error("native PRoot probe UID is already active")
        cases = [("native-proot", ["/opt/path-claude/claude"], True, False)]
    diagnostics = out / "observe-undici.cjs"
    diagnostics.write_text('''// Passive network diagnostics: no request headers or body are captured.
'use strict';
const fs = require('node:fs');
require('node:diagnostics_channel').channel('undici:request:error').subscribe(({request, error}) => {
  fs.appendFileSync(process.env.CC_TIMEOUT_PROBE_DIAGNOSTICS_FILE, JSON.stringify({
    ts: Date.now() / 1000, origin: String(request.origin), name: error.name,
    message: error.message, code: error.code, cause_code: error.cause?.code
  }) + '\\n');
});
''')
    events = []
    states = {name: {"stream": 0, "nonstream": 0} for name, *_ in cases}
    lock = threading.Lock()

    def record(**row):
        with lock:
            events.append({"ts": time.time(), **row})
            (out / "events.json").write_text(json.dumps(events, indent=2) + "\n")

    class Fixture(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            name = self.path.strip("/").split("/")[0]
            if self.path.endswith("count_tokens"):
                self.respond(b'{"input_tokens":64}', "application/json")
                return
            stream = bool(request.get("stream"))
            kind = "stream" if stream else "nonstream"
            with lock:
                states[name][kind] += 1
                number = states[name][kind]
            record(case=name, event="request", kind=kind, number=number,
                   max_tokens=request.get("max_tokens"))
            message = {"id": "msg_delayed_headers", "type": "message", "role": "assistant",
                       "model": "kimi-k3.1-pro", "content": [{"type": "text", "text": MARKER}],
                       "stop_reason": "end_turn", "stop_sequence": None,
                       "usage": {"input_tokens": 64, "output_tokens": 8}}
            if name == "native-proot" and sum(states[name].values()) == 2:
                delayed_body = json.dumps(message).encode()
                if args.delay_phase == "body":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(delayed_body) + 1))
                    self.end_headers()
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    record(case=name, event="body_headers_sent", number=number)
                deadline = time.monotonic() + args.delay_seconds
                while time.monotonic() < deadline:
                    if select.select([self.connection], [], [], .2)[0]:
                        if not self.connection.recv(1, socket.MSG_PEEK):
                            record(case=name, event="peer_closed_during_delay", phase=args.delay_phase,
                                   kind=kind, number=number)
                            self.close_connection = True
                            return
                record(case=name, event="delay_completed", seconds=args.delay_seconds)
                if args.delay_phase == "body":
                    try:
                        self.wfile.write(delayed_body)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
            if stream and number == 1:
                data = (sse("message_start", {"type": "message_start", "message": {
                    **message, "content": [], "stop_reason": None}})
                    + sse("content_block_start", {"type": "content_block_start", "index": 0,
                        "content_block": {"type": "text", "text": ""}})
                    + sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": "CONTROLLED_PARTIAL"}})
                    + sse("error", {"type": "error", "error": {"type": "api_error",
                        "message": "Controlled stream interruption"}})).encode()
                self.respond(data, "text/event-stream")
            elif not stream:
                if number == 1 and name != "native-proot":
                    deadline = time.monotonic() + args.delay_seconds
                    while time.monotonic() < deadline:
                        if select.select([self.connection], [], [], .2)[0]:
                            if not self.connection.recv(1, socket.MSG_PEEK):
                                record(case=name, event="peer_closed_before_headers", kind=kind,
                                       number=number)
                                self.close_connection = True
                                return
                    record(case=name, event="delay_completed", seconds=args.delay_seconds)
                self.respond(json.dumps(message).encode(), "application/json")
            else:
                data = (sse("message_start", {"type": "message_start", "message": {
                    **message, "content": [], "stop_reason": None}})
                    + sse("content_block_start", {"type": "content_block_start", "index": 0,
                        "content_block": {"type": "text", "text": ""}})
                    + sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": MARKER}})
                    + sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                    + sse("message_delta", {"type": "message_delta", "delta": {
                        "stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 8}})
                    + sse("message_stop", {"type": "message_stop"})).encode()
                self.respond(data, "text/event-stream")

        def respond(self, data, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def run(case):
        name, cli, process_env, adapter = case
        folder = out / name
        folder.mkdir()
        env = dict(os.environ)
        for key in ("API_TIMEOUT_MS", "NODE_OPTIONS", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
            env.pop(key, None)
        if process_env:
            env["API_TIMEOUT_MS"] = "29400000"
        if adapter:
            env["NODE_OPTIONS"] = "--require=" + str(args.adapter.resolve())
            env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{server.server_port}/{name}"
        if name.startswith("node-"):
            cli = cli[:1] + ["--require", str(diagnostics)] + cli[1:]
            env["CC_TIMEOUT_PROBE_DIAGNOSTICS_FILE"] = str(folder / "undici-errors.jsonl")
        settings = {"env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}/{name}",
            "ANTHROPIC_AUTH_TOKEN": "local-timeout-probe", "ANTHROPIC_MODEL": "kimi-k3.1-pro",
            "CLAUDE_CONFIG_DIR": str(folder / "config"), "CLAUDE_CODE_ALLOW_OTHER_MODELS": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000", "API_TIMEOUT_MS": "29400000",
            "CLAUDE_CODE_EXTRA_BODY": json.dumps({"max_tokens": 131072,
                "output_config": {"effort": "low"}}), "DISABLE_AUTOUPDATER": "1",
            "DISABLE_AUTO_COMPACT": "1"}, "enabledPlugins": {}}
        if args.native_root:
            if args.settings_env:
                production = json.loads(args.settings_env.read_text())["env"]
                protected = {key: settings["env"][key] for key in (
                    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CONFIG_DIR")}
                settings["env"] = {**production, **protected, "DISABLE_AUTOUPDATER": "1",
                                   "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_AUTO_COMPACT": "1"}
            if args.disable_native_idle_timeout:
                settings["env"]["API_FORCE_IDLE_TIMEOUT"] = "0"
            settings["env"]["CLAUDE_CONFIG_DIR"] = f"/home/agent/{out.name}"
        (folder / "settings.json").write_text(json.dumps(settings))
        (folder / "mcp.json").write_text('{"mcpServers":{}}')
        command = cli + ["-p", "Complete this controlled local transport fixture.",
            "--output-format", "stream-json", "--verbose", "--settings", str(folder / "settings.json"),
            "--setting-sources", "", "--strict-mcp-config", "--mcp-config", str(folder / "mcp.json"),
            "--permission-mode", "bypassPermissions", "--disable-slash-commands", "--tools", "",
            "--model", "kimi-k3.1-pro", "--effort", "low", "--max-turns", "3",
            "--debug-file", str(folder / "debug.log")]
        if args.native_root:
            guest_config = args.native_root / "eval" / out.name
            guest_config.mkdir(parents=True)
            shutil.copy2(folder / "settings.json", guest_config / "settings.json")
            shutil.copy2(folder / "mcp.json", guest_config / "mcp.json")
            command[command.index("--settings") + 1] = f"/eval/{out.name}/settings.json"
            command[command.index("--mcp-config") + 1] = f"/eval/{out.name}/mcp.json"
            command[command.index("--debug-file") + 1] = f"/home/agent/{out.name}-debug.log"
            command = ["setpriv", f"--reuid={args.uid}", f"--regid={args.uid}", "--clear-groups",
                       "--no-new-privs", args.proot, "-r", str(args.native_root.resolve()), "-b", "/proc",
                       "-w", "/app", "/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin",
                       "HOME=/home/agent", "LANG=C.UTF-8", "API_TIMEOUT_MS=29400000",
                       f"ANTHROPIC_BASE_URL=http://127.0.0.1:{server.server_port}/{name}",
                       *(["API_FORCE_IDLE_TIMEOUT=0"] if args.disable_native_idle_timeout else []), *command]
        started = time.time()
        with (folder / "cc.jsonl").open("w") as log:
            child = subprocess.Popen(command, env=env, cwd=args.native_root or folder,
                                     stdout=log, stderr=subprocess.STDOUT)
            (folder / "state.json").write_text(json.dumps({"pid": child.pid, "started_at": started}))
            try:
                code = child.wait(timeout=args.delay_seconds + 120)
            except subprocess.TimeoutExpired:
                child.kill()
                code = child.wait()
            finally:
                if args.native_root:
                    kill_uid(args.uid)
        result = None
        for line in (folder / "cc.jsonl").read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                result = event
        row = {"case": name, "process_environment_timeout": process_env,
               "adapter": adapter, "exit_code": code, "duration_seconds": round(time.time() - started, 3),
               "answer": (result or {}).get("result"), "is_error": (result or {}).get("is_error"),
               "success": code == 0 and not (result or {}).get("is_error", True)
                   and MARKER in (result or {}).get("result", ""), "request_counts": states[name]}
        with lock:
            case_events = [event for event in events if event.get("case") == name]
        row["delay_phase"] = args.delay_phase
        row["delay_completed"] = any(event["event"] == "delay_completed" for event in case_events)
        row["closed_during_delay"] = any(
            event["event"] in ("peer_closed_before_headers", "peer_closed_during_delay")
            for event in case_events)
        if args.delay_phase == "headers":
            row["received_delayed_headers"] = row["delay_completed"]
            row["closed_before_delayed_headers"] = row["closed_during_delay"]
        if adapter or args.native_root:
            row["success"] = (row["success"] and row["delay_completed"]
                and not row["closed_during_delay"]
                and (args.native_root is not None or states[name]["nonstream"] == 1))
        (folder / "result.json").write_text(json.dumps(row, indent=2) + "\n")
        print(json.dumps(row), flush=True)
        return row

    identity = {"probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "pinned_cli_sha256": hashlib.sha256(args.pinned_cli.read_bytes()).hexdigest(),
        "node_version": (subprocess.check_output(pinned[:1] + ["--version"], text=True).strip()
                         if not args.native_root else None),
        "delay_seconds": args.delay_seconds, "nvidia_requests": 0}
    identity.update(delay_phase=args.delay_phase, native_idle_timeout_disabled=args.disable_native_idle_timeout,
                    production_environment_supplied=bool(args.settings_env))
    if args.native_root:
        identity["native_cli_version"] = subprocess.check_output([str(args.pinned_cli), "--version"], text=True).strip()
    if args.adapter:
        identity["adapter_sha256"] = hashlib.sha256(args.adapter.read_bytes()).hexdigest()
    (out / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    (out / "probe-source.py").write_bytes(Path(__file__).read_bytes())
    try:
        with ThreadPoolExecutor(max_workers=len(cases)) as pool:
            rows = list(pool.map(run, cases))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    (out / "result.json").write_text(json.dumps({"results": rows, "nvidia_requests": 0}, indent=2) + "\n")
    if not all(row["success"] for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
