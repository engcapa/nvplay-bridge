"""Bounded real Playground tests. Requires Brave CDP; starts/stops its own HTTP server."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import threading
import time

os.environ.setdefault("UPSTREAM_KIND", "browser")

import httpx
import uvicorn
import browser_upstream as bu
import nvplay_bridge as bridge


async def main():
    port = int(os.getenv("BROWSER_CDP_PORT", "9225"))
    # Fail before starting a test server if the browser is unavailable.
    bu.LOCAL_HTTP.open(f"http://127.0.0.1:{port}/json/version", timeout=3).close()
    pool = bu.BrowserPool(port=port, size=3, prefix="kstreamtest")
    bridge._browser = pool
    bridge.BROWSER_TIMEOUT = 180
    bridge.BROWSER_DEBUG_LOG = str(Path(__file__).resolve().parents[1] / "logs" / "live-streaming.jsonl")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(bridge.app, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    while not server.started:
        await asyncio.sleep(0.01)

    async def request(prompt, *, tools=None, cancel=False):
        body = {"model": "kimi-k3.1-pro", "stream": True, "max_tokens": 2048,
                "output_config": {"effort": "max" if cancel else "low"},
                "messages": [{"role": "user", "content": prompt}]}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = {"type": "tool", "name": tools[0]["name"]}
        started = time.monotonic()
        first = None
        events = []
        async with httpx.AsyncClient(trust_env=False, timeout=200) as client:
            async with client.stream("POST", base + "/v1/messages", json=body) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    event = json.loads(line[5:])
                    events.append(event)
                    if event["type"] == "error":
                        raise AssertionError(event)
                    if event["type"] == "content_block_delta" and first is None:
                        first = time.monotonic() - started
                        print(f"first delta after {first:.3f}s", flush=True)
                        if cancel:
                            break
        elapsed = time.monotonic() - started
        assert first is not None, events
        if not cancel:
            assert events[-1]["type"] == "message_stop", events[-1]
        return events, first, elapsed

    try:
        events, first, total = await request("Compute 137 * 293 with one short check. End your answer with STREAM_OK.")
        text = "".join(e.get("delta", {}).get("text", "") for e in events)
        assert "STREAM_OK" in text, text
        assert first < total, (first, total)
        print(f"PASS real SSE: first={first:.3f}s total={total:.3f}s text={text!r}", flush=True)

        tools = [{"name": "weather", "description": "Get weather for a city", "input_schema": {
            "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
        events, first, total = await request("Call weather for Shanghai. Do not invent weather data.", tools=tools)
        args = "".join(e.get("delta", {}).get("partial_json", "") for e in events)
        assert json.loads(args)["city"], args
        assert any(e.get("content_block", {}).get("name") == "weather" for e in events)
        print(f"PASS real tool: first={first:.3f}s total={total:.3f}s args={args}", flush=True)

        await request("Prove that there are infinitely many primes. Think carefully about each step.", cancel=True)
        until = time.monotonic() + 10
        while pool._queue.qsize() != 3 and time.monotonic() < until:
            await asyncio.sleep(0.05)
        assert pool._queue.qsize() == 3, "cancelled request still holds a pool tab"
        states = []
        for upstream in pool._ups:
            state = upstream._eval("window.__bridge ? ({done:window.__bridge.done,pending:window.__bridge.pending}) : null")
            states.append(state)
            assert state is None or (state["done"] and state["pending"] == 0), state
        print(f"PASS real cancellation: all tabs released, states={states}", flush=True)

        results = await asyncio.gather(*(request(f"Reply exactly PARALLEL_{n}.") for n in range(3)))
        for n, (events, _, _) in enumerate(results):
            text = "".join(e.get("delta", {}).get("text", "") for e in events)
            assert f"PARALLEL_{n}" in text, text
        print("PASS real 3-tab concurrency without mixed responses", flush=True)
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 10)
        pool.close()
        listener.close()
        try:
            with bu.LOCAL_HTTP.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
                pages = json.load(response)
            for page in pages:
                if page.get("type") == "page" and "?kstreamtest=" in page.get("url", ""):
                    bu.LOCAL_HTTP.open(f"http://127.0.0.1:{port}/json/close/{page['id']}", timeout=5).close()
        except OSError as exc:
            print(f"browser cleanup unavailable: {exc}", flush=True)
        print("test server stopped and temporary tabs closed", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
