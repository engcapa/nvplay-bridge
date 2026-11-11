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
from native_eval_support import cc_result, kill_uid, write_json
import nvplay_bridge as bridge

MODEL = "kimi-k3.1-pro[1m]"
MARKER = "CONTROLLED_CONTEXT_OK"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pinned-cli", type=Path, required=True)
    parser.add_argument("--native-root", type=Path,
                        help="Run the exact native binary at /opt/path-claude/claude using PRoot")
    parser.add_argument("--uid", type=int, help="Dedicated ordinary UID for the native probe")
    parser.add_argument("--proot", default="proot", help="Verified PRoot executable (native mode only)")
    args = parser.parse_args()
    if args.native_root and (os.geteuid() != 0 or args.uid is None or not 20000 <= args.uid <= 29999):
        parser.error("native probes need sudo and a dedicated evaluation UID")
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
            config_directory = directory
            if args.native_root:
                config_directory = args.native_root / "eval" / out.name / case
                config_directory.mkdir(parents=True)
                env["CLAUDE_CONFIG_DIR"] = f"/home/agent/probe-{case}"
            write_json(config_directory / "settings.json", {"env": env, "model": MODEL, "enabledPlugins": {}})
            write_json(config_directory / "mcp.json", {"mcpServers": {}})
            bootstrap = (["/opt/path-claude/claude"] if args.native_root else
                         [shutil.which("node"), str(args.pinned_cli.resolve())])
            config_path = f"/eval/{out.name}/{case}" if args.native_root else str(directory)
            command = bootstrap + ["-p",
                "Execute the provided local Bash tool once, then finish with CONTROLLED_CONTEXT_OK.",
                "--output-format", "stream-json", "--verbose", "--settings", f"{config_path}/settings.json",
                "--setting-sources", "", "--strict-mcp-config", "--mcp-config", f"{config_path}/mcp.json",
                "--permission-mode", "bypassPermissions", "--disable-slash-commands", "--tools", "Bash",
                "--model", MODEL, "--effort", "high", "--max-turns", "10"]
            if args.native_root:
                if enabled:
                    command += ["--autocompact", "720000"]
                command = ["setpriv", f"--reuid={args.uid}", f"--regid={args.uid}", "--clear-groups",
                           "--no-new-privs", args.proot, "-r", str(args.native_root.resolve()),
                           "-b", "/proc", "-w", "/app", "/usr/bin/env", "-i",
                           "PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/home/agent",
                           "LANG=C.UTF-8", *(f"{key}={value}" for key, value in env.items()), *command]
            # Isolate from the host's CC environment, while retaining its executable/library paths.
            clean = {k: v for k, v in os.environ.items()
                     if not k.startswith(("CLAUDE", "ANTHROPIC", "DISABLE_AUTO_COMPACT", "DISABLE_COMPACT"))}
            with (directory / "cc.jsonl").open("wb") as log:
                started = time.time()
                try:
                    process = subprocess.run(command, cwd=args.native_root or directory, env={**clean, **env},
                                             stdout=log, stderr=subprocess.STDOUT, timeout=180)
                finally:
                    if args.native_root:
                        kill_uid(args.uid)
            client = cc_result(directory / "cc.jsonl") or {}
            events = []
            for line in (directory / "cc.jsonl").read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
            boundaries = sum(e.get("type") == "system" and e.get("subtype") == "compact_boundary" for e in events)
            compact_attempts = sum(e.get("type") == "system" and e.get("status") == "compacting" for e in events)
            compact_failures = [e.get("compact_error") for e in events if e.get("compact_result") == "failed"]
            windows = sorted({v.get("contextWindow") for v in client.get("modelUsage", {}).values()
                              if v.get("contextWindow")})
            requests = list(state["requests"])
            passed = (process.returncode == 0 and not client.get("is_error", True)
                      and MARKER in client.get("result", "") and windows == [1000000]
                      and all(r["bridge_alias_accepted"] and r["max_tokens"] == 131072 for r in requests)
                      and bool(compact_attempts if args.native_root else boundaries) == (enabled and tokens > 687000)
                      and (not compact_failures or (args.native_root and compact_failures == ["too_few_groups"])))
            row = {"case": case, "passed": passed, "exit_code": process.returncode,
                   "seconds": round(time.time() - started, 3), "synthetic_input_tokens": tokens,
                   "automatic_compaction_enabled": enabled, "configured_window_tokens": 720000,
                   "reported_context_windows": windows, "compaction_boundaries": boundaries,
                   "compaction_attempts": compact_attempts, "compaction_failures": compact_failures,
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
    if args.native_root:
        result["scope"] = "Native client tool execution, output/context configuration, and compaction trigger decisions; tiny fixture may have too few groups to complete compaction"
    write_json(out / "result.json", result)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
