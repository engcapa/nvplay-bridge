"""Opt-in real Playground + local Codex E2E. Uses separate tabs/config/workspace.

Run with the repository and optional vendor directory on PYTHONPATH:
    python tests/live_responses.py --codex
Never loads or changes the user's Codex configuration or credentials.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request

os.environ.setdefault("UPSTREAM_KIND", "browser")
import httpx
import uvicorn
import browser_upstream as bu
import nvplay_bridge as bridge

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "logs" / "responses-e2e-report.json"
RESULTS = []


def record(name, **details):
    result = {"test": name, "passed": True, **details}
    RESULTS.append(result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def text_of(response):
    return "".join(part["text"] for item in response["output"] if item["type"] == "message"
                   for part in item["content"] if part["type"] == "output_text")


async def streamed(client, base, body, *, cancel=False):
    body = {"model": "kimi-k3.1-pro", "stream": True, "store": False,
            "max_output_tokens": 2048, "reasoning": {"effort": "low"}, **body}
    started, first, events = time.monotonic(), None, []
    async with client.stream("POST", base + "/v1/responses", json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            assert event["sequence_number"] == len(events), event
            events.append(event)
            assert event["type"] not in ("error", "response.failed"), event
            if event["type"].endswith(".delta") and first is None:
                first = round(time.monotonic() - started, 3)
                if cancel:
                    break
    elapsed = round(time.monotonic() - started, 3)
    if not cancel:
        assert events[-1]["type"] in ("response.completed", "response.incomplete"), events[-1]
        assert first is not None and first < elapsed, (first, elapsed)
    return events, first, elapsed


async def codex_check(base):
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("--codex requested but codex is not on PATH")
    # Record hashes without reading or copying credentials into the test home.
    configured_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    paths = [configured_home / name for name in ("config.toml", "auth.json")]
    fingerprints = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in paths}
    run_root = ROOT / "logs"
    with tempfile.TemporaryDirectory(prefix="codex-responses-e2e-", dir=run_root) as task_directory:
        task_root = Path(task_directory)
        task_home = task_root / "home"
        workspace = task_root / "workspace"
        task_home.mkdir(); workspace.mkdir()
        (workspace / "numbers.txt").write_text("17\n25\n", encoding="utf-8")
        (task_home / "config.toml").write_text(f'''model = "kimi-k3.1-pro"
model_provider = "nvplay_e2e"
model_reasoning_effort = "low"
model_context_window = 262144
model_auto_compact_token_limit = 240000
approval_policy = "never"
default_permissions = ":workspace"
web_search = "disabled"
[features]
multi_agent = false
shell_snapshot = false
[model_providers.nvplay_e2e]
name = "nvplay isolated Responses E2E"
base_url = "{base}/v1"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
stream_idle_timeout_ms = 180000
''' + ('\n[windows]\nsandbox = "mxc"\n' if sys.platform == "win32" else '')
            + '\n[projects.' + json.dumps(str(workspace)) + ']\ntrust_level = "trusted"\n', encoding="utf-8")
        prompt = ("Work only in the current directory. Do not spawn agents, use the network, or ask questions. "
                  "Use exec_command to read numbers.txt, calculate the sum of its integer lines, and write result.txt "
                  "containing that decimal sum followed by a newline. Then use exec_command to read result.txt and "
                  "verify its contents. You MUST actually call the tools; do not just describe commands. "
                  "If a command is blocked by policy, stop and report the failure without trying alternate commands. "
                  "Finally reply with CODEX_RESPONSES_OK and the verified sum.")
        env = dict(os.environ)
        # Only the child process receives this temporary home; parent/global config is untouched.
        env["CODEX_HOME"] = str(task_home)
        if sys.platform == "win32":
            # Verify the restricted Windows sandbox before spending real inference.
            # This neither provisions the elevated sandbox nor changes saved user settings.
            probe = await asyncio.create_subprocess_exec(
                codex, "sandbox", "--include-managed-config", "--permission-profile", ":workspace",
                "-C", str(workspace), "--", "cmd.exe", "/d", "/c", "echo CODEX_SANDBOX_OK",
                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            probe_out, probe_err = await asyncio.wait_for(probe.communicate(), timeout=45)
            assert probe.returncode == 0 and b"CODEX_SANDBOX_OK" in probe_out, (
                "Restricted MXC sandbox is unavailable: " + probe_err.decode("utf-8", "replace"))
        args = [codex, "exec", "--ephemeral", "--skip-git-repo-check", "--json",
                "-C", str(workspace), "-o", str(task_root / "final.txt"), prompt]
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(*args, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
        except asyncio.TimeoutError:
            process.kill()
            stdout, stderr = await process.communicate()
            raise AssertionError("Local Codex E2E exceeded 300 seconds") from None
        (run_root / "codex-responses-e2e.stdout.jsonl").write_bytes(stdout)
        (run_root / "codex-responses-e2e.stderr.log").write_bytes(stderr)
        final = (task_root / "final.txt").read_text(encoding="utf-8") if (task_root / "final.txt").exists() else ""
        events = [json.loads(line) for line in stdout.decode("utf-8", "replace").splitlines() if line.startswith("{")]
        tool_events = [e for e in events if e.get("item", {}).get("type") == "command_execution"]
        completed_tools = [e for e in tool_events if e["type"] == "item.completed" and e["item"].get("exit_code") == 0]
        assert process.returncode == 0, stderr.decode("utf-8", "replace")[-3000:]
        assert (workspace / "result.txt").exists(), "Codex did not create result.txt: " + final
        assert (workspace / "result.txt").read_text(encoding="utf-8-sig").strip() == "42", final
        assert "CODEX_RESPONSES_OK" in final and "42" in final, final
        assert len(completed_tools) >= 2, events
        current = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in paths}
        assert current == fingerprints, "Existing Codex configuration/auth changed during isolated test"
        record("local_codex_tools_and_file_verification", seconds=round(time.monotonic() - started, 3),
               command_events=len(tool_events), successful_commands=len(completed_tools),
               final=final.strip(), original_config_unchanged=True)


async def extended_checks(base):
    async with httpx.AsyncClient(trust_env=False, timeout=250) as client:
        custom = {"type": "custom", "name": "echo_raw", "description": "Echo the complete raw input verbatim.", "format": {"type": "text"}}
        initial = "Call echo_raw with raw input exactly: 中文🙂\nsecond line\nAfter the tool returns, reply with its entire output verbatim."
        events, _, _ = await streamed(client, base, {"input": initial, "tools": [custom],
            "tool_choice": {"type": "custom", "name": "echo_raw"}})
        output = events[-1]["response"]["output"]
        call = next(item for item in output if item["type"] == "custom_tool_call")
        events, _, _ = await streamed(client, base, {"input": [{"role": "user", "content": initial}] + output + [
            {"type": "custom_tool_call_output", "call_id": call["call_id"], "output": "CUSTOM_RESULT_481"}],
            "tools": [custom], "tool_choice": "none"})
        assert "CUSTOM_RESULT_481" in text_of(events[-1]["response"]), events[-1]
        record("real_custom_tool_output_roundtrip")

        lookup = {"type": "function", "name": "lookup", "description": "Look up a supplied key.",
                  "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}}
        initial = ("Call lookup twice in parallel, once for key north and once for key south. Both keys are independent; "
                   "issue BOTH calls in this response. When the results arrive, report BOTH tool outputs verbatim.")
        events, _, _ = await streamed(client, base, {"input": initial, "tools": [lookup], "tool_choice": "required",
                                                    "parallel_tool_calls": True})
        output = events[-1]["response"]["output"]
        calls = [item for item in output if item["type"] == "function_call"]
        assert sorted(json.loads(call["arguments"])["key"] for call in calls) == ["north", "south"], calls
        assert len({call["call_id"] for call in calls}) == 2, calls
        results = [{"type": "function_call_output", "call_id": call["call_id"],
                    "output": "VALUE_" + json.loads(call["arguments"])["key"].upper()} for call in calls]
        events, _, _ = await streamed(client, base, {"input": [{"role": "user", "content": initial}] + output + results,
                                                    "tools": [lookup], "tool_choice": "none"})
        final = text_of(events[-1]["response"])
        assert "VALUE_NORTH" in final and "VALUE_SOUTH" in final, final
        record("real_parallel_function_calls_and_results", calls=len(calls))

        schema = {"type": "object", "properties": {"sum": {"type": "integer"}, "marker": {"type": "string"}},
                  "required": ["sum", "marker"], "additionalProperties": False}
        events, _, _ = await streamed(client, base, {"input": "Compute 17+25. Return JSON with sum=42 and marker=SCHEMA_OK.",
            "text": {"format": {"type": "json_schema", "name": "sum_result", "schema": schema, "strict": True}}})
        parsed = json.loads(text_of(events[-1]["response"]))
        assert parsed == {"sum": 42, "marker": "SCHEMA_OK"}, parsed
        record("real_json_schema_output", value=parsed)


async def main(run_codex, codex_only=False, extended_only=False):
    pool = bu.BrowserPool(port=int(os.getenv("BROWSER_CDP_PORT", "9225")), size=3, prefix="responsee2e", trace=bridge._dbg)
    bridge._browser = pool
    bridge.BROWSER_TIMEOUT = 240
    bridge.BROWSER_DEBUG_LOG = str(ROOT / "logs" / "responses-e2e-debug.jsonl")
    listener = socket.socket(); listener.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(bridge.app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    while not server.started:
        await asyncio.sleep(0.01)
    failure = None
    try:
        if codex_only or extended_only:
            if REPORT.exists():
                RESULTS.extend(json.loads(REPORT.read_text(encoding="utf-8"))["results"])
            if codex_only:
                await codex_check(base)
            else:
                await extended_checks(base)
            return
        async with httpx.AsyncClient(trust_env=False, timeout=250) as client:
            events, first, total = await streamed(client, base, {"input": "Compute 137 * 293. End with RESPONSES_OK."})
            response = events[-1]["response"]
            assert "40141" in text_of(response) and "RESPONSES_OK" in text_of(response), response
            assert any(e["type"] == "response.reasoning_summary_text.delta" for e in events)
            assert response["usage"]["output_tokens"] > 0
            record("real_reasoning_and_text_stream", first_content_sec=first, total_sec=total, usage=response["usage"])

            stored = await client.post(base + "/v1/responses", json={"model": "kimi-k3.1-pro", "input": "Remember the code blue-orchid-731. Say READY.", "reasoning": {"effort": "low"}, "max_output_tokens": 512})
            stored.raise_for_status(); stored_id = stored.json()["id"]
            assert (await client.get(base + "/v1/responses/" + stored_id)).json() == stored.json()
            follow = await client.post(base + "/v1/responses", json={"model": "kimi-k3.1-pro", "previous_response_id": stored_id, "input": "What was the exact code?", "store": False, "reasoning": {"effort": "low"}, "max_output_tokens": 512})
            follow.raise_for_status()
            assert "blue-orchid-731" in text_of(follow.json()), follow.text
            assert (await client.delete(base + "/v1/responses/" + stored_id)).json()["deleted"]
            assert (await client.get(base + "/v1/responses/" + stored_id)).status_code == 404
            record("real_nonstream_stored_history_retrieve_delete")

            tools = [{"type": "namespace", "name": "utilities", "tools": [{"type": "function", "name": "lookup", "description": "Look up a supplied key.", "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}}]}]
            initial_input = "Call utilities.lookup with key phoenix."
            events, _, _ = await streamed(client, base, {"input": initial_input, "tools": tools,
                "tool_choice": {"type": "function", "namespace": "utilities", "name": "lookup"}})
            output = events[-1]["response"]["output"]
            call = next(i for i in output if i["type"] == "function_call")
            assert call["namespace"] == "utilities" and json.loads(call["arguments"])["key"] == "phoenix", call
            assert any(e["type"] == "response.function_call_arguments.delta" for e in events)
            events, _, _ = await streamed(client, base, {"input": [{"role": "user", "content": initial_input}] + output + [
                {"type": "function_call_output", "call_id": call["call_id"], "output": "LOOKUP_RESULT_882"}], "tools": tools, "tool_choice": "none"})
            assert "LOOKUP_RESULT_882" in text_of(events[-1]["response"]), events[-1]
            record("real_namespaced_function_and_tool_result")

            custom = {"type": "custom", "name": "echo_raw", "description": "Echo the complete raw input verbatim.", "format": {"type": "text"}}
            events, _, _ = await streamed(client, base, {"input": "Call echo_raw with raw input exactly: 中文🙂\nsecond line", "tools": [custom], "tool_choice": {"type": "custom", "name": "echo_raw"}})
            call = next(i for i in events[-1]["response"]["output"] if i["type"] == "custom_tool_call")
            delta = "".join(e["delta"] for e in events if e["type"] == "response.custom_tool_call_input.delta")
            assert delta == call["input"] and "中文🙂" in delta and "second line" in delta, call
            record("real_custom_tool_incremental_decoding", input=call["input"])

            events, _, _ = await streamed(client, base, {"input": "Prove there are infinitely many primes, carefully examine each step.", "reasoning": {"effort": "high"}}, cancel=True)
            until = time.monotonic() + 20
            while any(s in ("busy", "pending", "recovering") for s in pool.status()["tabs"].values()) and time.monotonic() < until:
                await asyncio.sleep(0.2)
            assert all(s == "ready" for s in pool.status()["tabs"].values()), pool.status()
            record("real_stream_disconnect_cleanup_and_recovery")

            async def concurrent(marker):
                ev, _, _ = await streamed(client, base, {"input": "Reply exactly " + marker + ".", "max_output_tokens": 512})
                value = text_of(ev[-1]["response"])
                assert marker in value, value
                return marker
            markers = await asyncio.gather(*(concurrent("CONCURRENT_" + str(i)) for i in range(3)))
            record("real_three_tab_concurrency", markers=markers)
        await extended_checks(base)
        if run_codex:
            await codex_check(base)
    except BaseException as exc:
        failure = {"type": type(exc).__name__, "message": str(exc)[-4000:]}
        raise
    finally:
        REPORT.write_text(json.dumps({"results": RESULTS, "failure": failure}, ensure_ascii=False, indent=2), encoding="utf-8")
        server.should_exit = True
        await asyncio.to_thread(thread.join, 8)
        await asyncio.to_thread(pool.close)
        # Test tabs use a distinct prefix; never close production kpool tabs.
        try:
            with bu.LOCAL_HTTP.open(f"http://127.0.0.1:{pool.port}/json/list", timeout=5) as response:
                pages = json.load(response)
            for page in pages:
                if page.get("type") == "page" and any(bu._matches_hint(page.get("url", ""), f"responsee2e={n}") for n in range(1, 4)):
                    bu.LOCAL_HTTP.open(f"http://127.0.0.1:{pool.port}/json/close/{page['id']}", timeout=5).close()
        except OSError:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", action="store_true", help="Also exercise the installed Codex CLI with an isolated home")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--codex-only", action="store_true", help="Rerun only the local Codex stage and retain earlier live results")
    group.add_argument("--extended-only", action="store_true", help="Rerun custom outputs, parallel calls and JSON schema; retain earlier live results")
    args = parser.parse_args()
    asyncio.run(main(args.codex, args.codex_only, args.extended_only))
