"""Exercise the pinned real CLI's context/compaction decisions using synthetic usage.

No upstream request or benchmark input is used. The fixture deliberately reports
a large input count after one tiny tool call; this checks client behavior, not
the provider's actual context capacity.
"""

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO))
from native_eval_support import cc_result, write_json
import nvplay_bridge as bridge

MODEL = "kimi-k3.1-pro[1m]"
MARKER = "CONTROLLED_CONTEXT_OK"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pinned-cli", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    state, rows = {}, []

    class Fixture(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if "count_tokens" in self.path:
                self.respond(b'{"input_tokens":64}', "application/json")
                return
            number = len(state["requests"]) + 1
            write_json(out / state["case"] / f"request-{number}.json", request)
            try:
                bridge.validate_model(request.get("model"))
                accepted = True
            except bridge.ModelNotSupported:
                accepted = False
            state["requests"].append({"number": number, "model": request.get("model"),
                "max_tokens": request.get("max_tokens"), "stream": request.get("stream", False),
                "bridge_alias_accepted": accepted,
                "system_sha256": hashlib.sha256(json.dumps(request.get("system")).encode()).hexdigest(),
                "message_count": len(request.get("messages", []))})
            first = number == 1
            usage = {"input_tokens": state["synthetic_input"] if first else 1000, "output_tokens": 8}
            block = ({"type": "tool_use", "id": "tool_context_fixture", "name": "Bash",
                      "input": {"command": "printf CC_CONTEXT_TOOL_OK", "description": "Controlled local tool"}}
                     if first else {"type": "text", "text": MARKER})
            message = {"id": f"msg_context_{number}", "type": "message", "role": "assistant",
                       "model": request["model"], "content": [block], "usage": usage,
                       "stop_reason": "tool_use" if first else "end_turn", "stop_sequence": None}
            if request.get("stream"):
                events = [("message_start", {"type": "message_start", "message": {
                    **message, "content": [], "stop_reason": None, "usage": {**usage, "output_tokens": 0}}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0,
                        "content_block": {**block, "input": {}} if first else {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                                  if first else {"type": "text_delta", "text": MARKER}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {
                        "stop_reason": message["stop_reason"], "stop_sequence": None}, "usage": {"output_tokens": 8}}),
                    ("message_stop", {"type": "message_stop"})]
                body = "".join(f"event: {kind}\ndata: {json.dumps(value)}\n\n" for kind, value in events).encode()
                self.respond(body, "text/event-stream")
            else:
                self.respond(json.dumps(message).encode(), "application/json")

        def respond(self, body, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for case, tokens, enabled in (("small-context", 1000, True),
                                      ("below-compact-trigger", 686000, True),
                                      ("automatic-compaction", 688000, True),
                                      ("disabled-control", 688000, False)):
            directory = out / case
            directory.mkdir()
            state.update(case=case, synthetic_input=tokens, requests=[])
            env = {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                   "ANTHROPIC_AUTH_TOKEN": "local-context-fixture-token", "ANTHROPIC_MODEL": MODEL,
                   "CLAUDE_CODE_ALLOW_OTHER_MODELS": "1", "CLAUDE_CONFIG_DIR": str(directory / "config"),
                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                   "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000", "API_TIMEOUT_MS": "29400000",
                   "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "720000", "DISABLE_AUTOUPDATER": "1",
                   "CLAUDE_CODE_EXTRA_BODY": json.dumps({"max_tokens": 131072,
                                                         "output_config": {"effort": "high"}})}
            if not enabled:
                env["DISABLE_AUTO_COMPACT"] = "1"
            write_json(directory / "settings.json", {"env": env, "model": MODEL, "enabledPlugins": {}})
            write_json(directory / "mcp.json", {"mcpServers": {}})
            command = [shutil.which("node"), str(args.pinned_cli.resolve()), "-p",
                "Execute the provided local Bash tool once, then finish with CONTROLLED_CONTEXT_OK.",
                "--output-format", "stream-json", "--verbose", "--settings", str(directory / "settings.json"),
                "--setting-sources", "", "--strict-mcp-config", "--mcp-config", str(directory / "mcp.json"),
                "--permission-mode", "bypassPermissions", "--disable-slash-commands", "--tools", "Bash",
                "--model", MODEL, "--effort", "high", "--max-turns", "10"]
            # Isolate from the host's CC environment, while retaining its executable/library paths.
            clean = {k: v for k, v in os.environ.items()
                     if not k.startswith(("CLAUDE", "ANTHROPIC", "DISABLE_AUTO_COMPACT", "DISABLE_COMPACT"))}
            with (directory / "cc.jsonl").open("wb") as log:
                started = time.time()
                process = subprocess.run(command, cwd=directory, env={**clean, **env},
                                         stdout=log, stderr=subprocess.STDOUT, timeout=180)
            client = cc_result(directory / "cc.jsonl") or {}
            events = []
            for line in (directory / "cc.jsonl").read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
            boundaries = sum(e.get("type") == "system" and e.get("subtype") == "compact_boundary" for e in events)
            windows = sorted({v.get("contextWindow") for v in client.get("modelUsage", {}).values()
                              if v.get("contextWindow")})
            requests = list(state["requests"])
            passed = (process.returncode == 0 and not client.get("is_error", True)
                      and MARKER in client.get("result", "") and windows == [1000000]
                      and all(r["bridge_alias_accepted"] and r["max_tokens"] == 131072 for r in requests)
                      and bool(boundaries) == (enabled and tokens > 687000))
            row = {"case": case, "passed": passed, "exit_code": process.returncode,
                   "seconds": round(time.time() - started, 3), "synthetic_input_tokens": tokens,
                   "automatic_compaction_enabled": enabled, "configured_window_tokens": 720000,
                   "reported_context_windows": windows, "compaction_boundaries": boundaries,
                   "requests": requests, "cc_is_error": client.get("is_error"),
                   "cc_log_sha256": hashlib.sha256((directory / "cc.jsonl").read_bytes()).hexdigest()}
            rows.append(row)
            write_json(directory / "result.json", row)
            print(json.dumps(row), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    result = {"passed": all(row["passed"] for row in rows), "cases": rows,
              "cli_sha256": hashlib.sha256(args.pinned_cli.read_bytes()).hexdigest(),
              "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "upstream_requests": 0, "actual_large_payload_capacity_tested": False}
    write_json(out / "result.json", result)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
