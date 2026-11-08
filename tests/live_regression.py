"""live_regression.py — 通过 HTTP 对 8083 做真实 API 回归。"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = "http://127.0.0.1:8083"
_HERE = os.path.dirname(os.path.abspath(__file__))
DEBUG = os.getenv("BROWSER_DEBUG_LOG") or os.path.join(
    os.path.dirname(_HERE), "logs", "bridge-debug.jsonl"
)


def post(body: dict, timeout: float = 180.0) -> httpx.Response:
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=10)) as client:
        return client.post(BASE + "/v1/messages", json=body)


def payload_after(ts: float) -> dict:
    events = [json.loads(line) for line in open(DEBUG, encoding="utf-8")]
    candidates = [e for e in events if e.get("event") == "payload" and e.get("ts", 0) >= ts]
    if not candidates:
        raise AssertionError("no payload debug event after request")
    return candidates[-1]


def text_of(resp: httpx.Response) -> str:
    return "".join(
        block.get("text", "")
        for block in (resp.json().get("content") or [])
        if block.get("type") == "text"
    )


def tool_of(resp: httpx.Response) -> dict | None:
    for block in resp.json().get("content") or []:
        if block.get("type") == "tool_use":
            return block
    return None


def check_models_and_allowlist() -> None:
    with httpx.Client(timeout=10) as client:
        models = client.get(BASE + "/v1/models").json()
    ids = {m["id"] for m in models["data"]}
    assert "kimi-k3.1-pro" in ids and "moonshotai/kimi-k3.1-pro" in ids, ids

    allowed = httpx.post(
        BASE + "/v1/messages/count_tokens",
        json={"model": "kimi-k3.1-pro", "messages": [{"role": "user", "content": "hi"}]},
        timeout=10,
    )
    assert allowed.status_code == 200, allowed.text

    for path in ("/v1/messages/count_tokens", "/v1/messages"):
        bad = httpx.post(
            BASE + path,
            json={"model": "claude-sonnet-4-5-20250929", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
            timeout=10,
        )
        assert bad.status_code == 404, (path, bad.status_code, bad.text)
        assert bad.json()["error"]["type"] == "not_found_error"
    print("PASS models/allowlist")


def check_no_forced_effort() -> None:
    ts = time.time()
    resp = post({"model": "kimi-k3.1-pro", "messages": [{"role": "user", "content": "Reply with exactly: NOEFFORT"}]})
    assert resp.status_code == 200, resp.text
    payload = payload_after(ts)
    assert payload["max_tokens"] == 131072, payload
    assert payload["requested_effort"] is None and payload["reasoning_effort"] is None, payload
    assert "NOEFFORT" in text_of(resp)
    print("PASS no forced effort / default 131072")


def check_effort_mapping() -> None:
    ts = time.time()
    resp = post(
        {
            "model": "kimi-k3.1-pro",
            "max_tokens": 128,
            "output_config": {"effort": "max"},
            "messages": [{"role": "user", "content": "Reply with exactly: EFFMAX"}],
        }
    )
    assert resp.status_code == 200, resp.text
    payload = payload_after(ts)
    assert payload["reasoning_effort"] == "max" and payload["requested_effort"] == "max", payload

    ts = time.time()
    resp = post(
        {
            "model": "kimi-k3.1-pro",
            "max_tokens": 128,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "xhigh"},
            "messages": [{"role": "user", "content": "Reply with exactly: EFFXHIGH"}],
        }
    )
    assert resp.status_code == 200, resp.text
    payload = payload_after(ts)
    assert payload["reasoning_effort"] == "max", payload
    print("PASS output_config effort max / xhigh->max")


def check_thinking_passback() -> None:
    resp = post(
        {
            "model": "kimi-k3.1-pro",
            "max_tokens": 128,
            "messages": [
                {"role": "user", "content": "What is 2+2? Answer only the number."},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "2+2 is 4."},
                        {"type": "text", "text": "4"},
                    ],
                },
                {"role": "user", "content": "Now 3+3? Answer only the number."},
            ],
        }
    )
    assert resp.status_code == 200, resp.text
    assert "6" in text_of(resp), text_of(resp)
    print("PASS assistant thinking passback")


def check_tool_roundtrip() -> None:
    tools = [
        {
            "name": "get_weather",
            "description": "Get current weather for a city",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]
    first = post(
        {
            "model": "kimi-k3.1-pro",
            "max_tokens": 256,
            "tools": tools,
            "tool_choice": {"type": "tool", "name": "get_weather"},
            "messages": [{"role": "user", "content": "Use get_weather to check San Francisco."}],
        }
    )
    assert first.status_code == 200, first.text
    body = first.json()
    tool = tool_of(first)
    assert tool is not None, body
    assert tool["name"] == "get_weather", tool

    second = post(
        {
            "model": "kimi-k3.1-pro",
            "max_tokens": 256,
            "tools": tools,
            "messages": [
                {"role": "user", "content": "Use get_weather to check San Francisco."},
                {"role": "assistant", "content": [tool]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool["id"],
                            "content": "Sunny, 18C in San Francisco.",
                        }
                    ],
                },
            ],
        }
    )
    assert second.status_code == 200, second.text
    assert second.json()["stop_reason"] == "end_turn", second.text
    assert "18" in text_of(second) or "Sunny" in text_of(second), text_of(second)
    print("PASS tool roundtrip")


def check_stream() -> None:
    with httpx.Client(timeout=httpx.Timeout(180, connect=10)) as client:
        with client.stream(
            "POST",
            BASE + "/v1/messages",
            json={
                "model": "kimi-k3.1-pro",
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "Reply with exactly: STREAMOK"}],
            },
        ) as resp:
            assert resp.status_code == 200
            raw = "".join(resp.iter_text())
    assert "event: message_start" in raw, raw[:500]
    assert "content_block_delta" in raw, raw[:500]
    assert "event: message_stop" in raw, raw[:500]
    text_parts: list[str] = []
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if event.get("type") == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                text_parts.append(delta.get("text", ""))
    assert "STREAMOK" in "".join(text_parts), text_parts
    print("PASS SSE stream")


def check_concurrency() -> None:
    def one(i: int) -> tuple[int, float, str]:
        start = time.time()
        resp = post(
            {
                "model": "kimi-k3.1-pro",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": f"Reply with exactly: PAR{i}"}],
            }
        )
        return resp.status_code, time.time() - start, text_of(resp)

    wall = time.time()
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(one, (1, 2, 3)))
    elapsed = time.time() - wall
    assert all(status == 200 for status, _, _ in results), results
    assert all(f"PAR{i}" in text for i, (_, _, text) in enumerate(results, 1)), results
    total = sum(t for _, t, _ in results)
    assert elapsed < total * 0.85, (elapsed, total)
    print(f"PASS concurrency wall={elapsed:.1f}s individual={[round(t,1) for _,t,_ in results]}")


if __name__ == "__main__":
    check_models_and_allowlist()
    check_no_forced_effort()
    check_effort_mapping()
    check_thinking_passback()
    check_tool_roundtrip()
    check_stream()
    check_concurrency()
    print("ALL LIVE REGRESSION PASSED")
