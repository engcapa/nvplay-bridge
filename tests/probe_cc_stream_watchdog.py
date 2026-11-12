"""Opt-in native CC idle probe using only a localhost Messages fixture.

Send message_start and regular pings, delay all content, then finish normally.
Compare the production environment with a longer event idle budget. No model,
benchmark, browser, or external service is used, and CC has no wall-clock cutoff.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from native_eval_support import allocate_uid, write_json


MARKER = "LOCAL_STREAM_IDLE_PROBE_COMPLETE"


def sse(kind, value):
    return f"event: {kind}\ndata: {json.dumps(value)}\n\n".encode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--delay-seconds", type=float, default=660)
    parser.add_argument("--extended-idle-ms", type=int, default=29400000)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("use sudo; both fixture clients run under new ordinary UIDs")
    if args.delay_seconds <= 0 or args.extended_idle_ms <= args.delay_seconds * 1000:
        parser.error("require a positive delay shorter than the extended idle budget")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cli = args.cli.resolve(strict=True)
    production = json.loads(args.settings.read_text())["env"]
    lock = threading.Lock()
    events, requests = [], {"production": [], "extended": []}

    def emit(case, event, **metadata):
        with lock:
            row = {"ts": time.time(), "case": case, "event": event, **metadata}
            events.append(row)
            with (output / "events.jsonl").open("a") as stream:
                stream.write(json.dumps(row) + "\n")

    class Fixture(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            case = self.path.strip("/").split("/")[0]
            if self.path.endswith("count_tokens"):
                self.respond(b'{"input_tokens":64}', "application/json")
                return
            if case not in requests:
                self.send_error(404)
                return
            streaming = bool(body.get("stream"))
            with lock:
                ordinal = len(requests[case]) + 1
                requests[case].append({"stream": streaming, "max_tokens": body.get("max_tokens")})
            emit(case, "request", ordinal=ordinal, stream=streaming, max_tokens=body.get("max_tokens"))
            message = {"id": f"msg_{case}_{ordinal}", "type": "message", "role": "assistant",
                       "model": "kimi-k3.1-pro", "content": [{"type": "text", "text": MARKER}],
                       "stop_reason": "end_turn", "stop_sequence": None,
                       "usage": {"input_tokens": 64, "output_tokens": 8}}
            if not streaming:
                self.respond(json.dumps(message).encode(), "application/json")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(sse("message_start", {"type": "message_start", "message": {
                    **message, "content": [], "stop_reason": None}}))
                self.wfile.flush()
                emit(case, "message_start_sent", ordinal=ordinal)
                if ordinal == 1:
                    deadline = time.monotonic() + args.delay_seconds
                    while time.monotonic() < deadline:
                        self.wfile.write(sse("ping", {"type": "ping"}))
                        self.wfile.flush()
                        time.sleep(min(2, max(0, deadline - time.monotonic())))
                    emit(case, "delay_completed", ordinal=ordinal, seconds=args.delay_seconds)
                self.wfile.write(
                    sse("content_block_start", {"type": "content_block_start", "index": 0,
                        "content_block": {"type": "text", "text": ""}})
                    + sse("content_block_delta", {"type": "content_block_delta", "index": 0,
                        "delta": {"type": "text_delta", "text": MARKER}})
                    + sse("content_block_stop", {"type": "content_block_stop", "index": 0})
                    + sse("message_delta", {"type": "message_delta", "delta": {
                        "stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 8}})
                    + sse("message_stop", {"type": "message_stop"}))
                self.wfile.flush()
                emit(case, "stream_completed", ordinal=ordinal)
            except (BrokenPipeError, ConnectionResetError):
                emit(case, "peer_closed_before_content", ordinal=ordinal)

        def respond(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def run(case):
        uid = allocate_uid(args.base.resolve())
        folder = output / case
        folder.mkdir()
        runtime = Path(tempfile.mkdtemp(prefix=f"nvplay-idle-probe-{uid}-", dir="/var/tmp"))
        runtime.chmod(0o750)
        os.chown(runtime, uid, uid)
        config = runtime / "settings.json"
        routing = f"http://127.0.0.1:{server.server_port}/{case}"
        settings = {"env": {**production, "ANTHROPIC_BASE_URL": routing,
            "ANTHROPIC_AUTH_TOKEN": "local-fixture", "CLAUDE_CONFIG_DIR": str(runtime / "config"),
            "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}, "enabledPlugins": {}}
        if case == "extended":
            settings["env"]["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] = str(args.extended_idle_ms)
        write_json(config, settings)
        (runtime / "mcp.json").write_text('{"mcpServers":{}}')
        command = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", "--no-new-privs",
            "/usr/bin/env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", f"HOME={runtime}", "LANG=C.UTF-8",
            f"API_TIMEOUT_MS={production['API_TIMEOUT_MS']}", "API_FORCE_IDLE_TIMEOUT=0",
            f"ANTHROPIC_BASE_URL={routing}", str(cli), "-p", "Complete this local fixture.",
            "--output-format", "stream-json", "--verbose", "--settings", str(config),
            "--setting-sources", "", "--strict-mcp-config", "--mcp-config", str(runtime / "mcp.json"),
            "--permission-mode", "bypassPermissions", "--disable-slash-commands", "--tools", "",
            "--model", "kimi-k3.1-pro[1m]", "--effort", "high", "--autocompact", "720000",
            "--debug-file", str(runtime / "debug.log")]
        started = time.time()
        with (folder / "cc.jsonl").open("w") as log:
            child = subprocess.Popen(command, cwd=runtime, stdout=log, stderr=subprocess.STDOUT)
            write_json(folder / "process.json", {"pid": child.pid, "uid": uid,
                "started_at": started, "runtime": str(runtime), "wall_clock_timeout_seconds": None})
            code = child.wait()
        terminal = {}
        for line in (folder / "cc.jsonl").read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                terminal = event
        debug = (runtime / "debug.log").read_text(errors="replace") if (runtime / "debug.log").is_file() else ""
        row = {"case": case, "uid": uid, "exit_code": code, "seconds": round(time.time() - started, 3),
               "client_success": code == 0 and terminal.get("is_error") is False
                   and MARKER in terminal.get("result", ""),
               "event_idle_ms": settings["env"].get("CLAUDE_STREAM_IDLE_TIMEOUT_MS"),
               "requests": requests[case], "idle_timeout_debug_present": "Streaming idle timeout:" in debug,
               "byte_idle_timeout_debug_present": "Streaming idle timeout (byte-level)" in debug,
               "sdk_timeout_debug_present": "Streaming timeout (SDK abort)" in debug,
               "stream_fallback_debug_present": "falling back to non-streaming" in debug.lower(),
               "delay_completed": any(e["case"] == case and e["event"] == "delay_completed" for e in events),
               "peer_closed_before_content": any(e["case"] == case and e["event"] == "peer_closed_before_content" for e in events)}
        write_json(folder / "result.json", row)
        print(json.dumps(row), flush=True)
        return row

    write_json(output / "identity.json", {"probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cli_sha256": hashlib.file_digest(cli.open("rb"), "sha256").hexdigest(),
        "delay_seconds": args.delay_seconds, "extended_idle_ms": args.extended_idle_ms,
        "external_requests": 0, "model_invocations": 0, "wall_clock_timeout_seconds": None})
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(run, ("production", "extended")))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    write_json(output / "result.json", {"results": rows, "external_requests": 0, "model_invocations": 0})
    if not all(row["client_success"] for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
