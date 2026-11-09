"""test_thinking_tool_loop.py — 验证 assistant thinking + tool_use 回传是否导致重复调用。"""

from __future__ import annotations

import json

import httpx

BASE = "http://127.0.0.1:8083"
TOOLS = [
    {
        "name": "get_weather",
        "description": "Get current weather",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]


def post(messages: list[dict]) -> dict:
    with httpx.Client(timeout=httpx.Timeout(180, connect=10)) as client:
        r = client.post(
            BASE + "/v1/messages",
            json={"model": "kimi-k3.1-pro", "max_tokens": 256, "tools": TOOLS, "messages": messages},
        )
    if r.status_code != 200:
        print("HTTP", r.status_code, r.text[:400])
        return {}
    return r.json()


def summarize(resp: dict) -> str:
    kinds = []
    for block in resp.get("content") or []:
        kinds.append(block.get("type") + (":" + block.get("name", "") if block.get("type") == "tool_use" else ""))
    return f"stop={resp.get('stop_reason')} blocks={kinds}"


def main() -> None:
    first = post([{"role": "user", "content": "Use get_weather to check San Francisco."}])
    print("first:", summarize(first))
    tool = next((b for b in first.get("content") or [] if b.get("type") == "tool_use"), None)
    if not tool:
        raise SystemExit("model did not call tool")

    base_messages = [
        {"role": "user", "content": "Use get_weather to check San Francisco."},
    ]
    tool_result = [
        {
            "type": "tool_result",
            "tool_use_id": tool["id"],
            "content": "Sunny, 18C in San Francisco.",
        }
    ]

    with_thinking = base_messages + [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "I should call get_weather for SF."},
                tool,
            ],
        },
        {"role": "user", "content": tool_result},
    ]
    without_thinking = base_messages + [
        {"role": "assistant", "content": [tool]},
        {"role": "user", "content": tool_result},
    ]

    print("with thinking:", summarize(post(with_thinking)))
    print("without thinking:", summarize(post(without_thinking)))


if __name__ == "__main__":
    main()
