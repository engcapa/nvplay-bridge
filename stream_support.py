"""Incremental SSE framing and ordered Anthropic content blocks."""
from __future__ import annotations

import json
import re
import tempfile

MAX_FRAME = 1024 * 1024
MAX_DEFERRED_OUTPUT = 64 * 1024 * 1024


class DeferredEvents:
    """Stage output on disk above 256 KiB; retain only the final usage/finish."""

    def __init__(self) -> None:
        self.file = tempfile.SpooledTemporaryFile(max_size=256 * 1024, mode="w+b")
        self.size = 0
        self.output_started = False
        self.usage: dict | None = None
        self.finish: dict | None = None

    def append(self, event: dict) -> None:
        if event["type"] == "usage":
            self.usage = event
            return
        if event["type"] == "finish":
            self.finish = event
            return
        data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        if self.size + len(data) > MAX_DEFERRED_OUTPUT:
            raise ValueError("deferred upstream output exceeds 64 MiB")
        self.file.write(data)
        self.size += len(data)
        self.output_started |= event["type"] in ("text", "tool")

    def __iter__(self):
        self.file.seek(0)
        for line in self.file:
            yield json.loads(line)
        if self.finish is not None:
            yield self.finish
        if self.usage is not None:
            yield self.usage

    def close(self) -> None:
        self.file.close()


class SSEDecoder:
    def __init__(self) -> None:
        self.buffer = ""
        self.data: list[str] = []
        self.size = 0
        self.ended = False

    def _line(self, line: str) -> list[dict]:
        if line:
            if line.startswith(":"):
                return []
            field, _, value = line.partition(":")
            if field == "data":
                self.data.append(value.removeprefix(" "))
                self.size += len(value.encode("utf-8"))
                if self.size > MAX_FRAME:
                    raise ValueError("upstream SSE frame exceeds 1 MiB")
            return []
        if not self.data:
            return []
        raw = "\n".join(self.data)
        self.data.clear()
        self.size = 0
        if self.ended:
            raise ValueError("upstream sent data after [DONE]")
        if raw == "[DONE]":
            self.ended = True
            return []
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("upstream SSE data must be an object")
        return [value]

    def feed(self, text: str, *, final: bool = False) -> list[dict]:
        self.buffer += text
        result: list[dict] = []
        while match := re.search(r"\r\n|\r|\n", self.buffer):
            if not final and match.group() == "\r" and match.end() == len(self.buffer):
                break
            result.extend(self._line(self.buffer[:match.start()]))
            self.buffer = self.buffer[match.end():]
        if len(self.buffer.encode("utf-8")) + self.size > MAX_FRAME:
            raise ValueError("upstream SSE frame exceeds 1 MiB")
        if final:
            if self.buffer:
                result.extend(self._line(self.buffer))
                self.buffer = ""
            result.extend(self._line(""))
        return result


class AnthropicBlocks:
    """Keep blocks contiguous; stream the first tool while buffering later tools."""
    def __init__(self) -> None:
        self.index = -1
        self.current: str | None = None
        self.tools: dict[int, dict] = {}
        self.active_tool: int | None = None
        self.usage: dict = {}
        self.reason: str | None = None
        self.retained = 0

    def _stop(self) -> list[tuple[str, dict]]:
        if self.current is None:
            return []
        self.current = None
        return [("content_block_stop", {"type": "content_block_stop", "index": self.index})]

    def _start(self, kind: str, block: dict) -> list[tuple[str, dict]]:
        result = self._stop()
        self.index += 1
        self.current = kind
        result.append(("content_block_start", {"type": "content_block_start", "index": self.index,
                                               "content_block": block}))
        return result

    def _delta(self, delta: dict) -> tuple[str, dict]:
        return "content_block_delta", {"type": "content_block_delta", "index": self.index, "delta": delta}

    def _tool_start(self, slot: dict) -> list[tuple[str, dict]]:
        if not slot["id"] or not slot["name"]:
            raise ValueError("upstream tool is missing id/name")
        slot["started"] = True
        result = self._start("tool", {"type": "tool_use", "id": slot["id"],
                                      "name": slot["name"], "input": {}})
        if slot["args"]:
            result.append(self._delta({"type": "input_json_delta", "partial_json": slot["args"]}))
        return result

    def feed(self, event: dict) -> list[tuple[str, dict]]:
        kind = event["type"]
        if kind == "usage":
            self.usage = event["usage"]
            return []
        if kind == "finish":
            self.reason = event["reason"]
            return []
        if kind == "ping":
            return [("ping", {"type": "ping"})]
        if kind in ("thinking", "text"):
            if self.tools:
                raise ValueError("upstream text/thinking interleaved after tool parameters")
            result = []
            if self.current != kind:
                block = ({"type": "thinking", "thinking": "", "signature": ""}
                         if kind == "thinking" else {"type": "text", "text": ""})
                result.extend(self._start(kind, block))
            delta = ({"type": "thinking_delta", "thinking": event["text"]}
                     if kind == "thinking" else {"type": "text_delta", "text": event["text"]})
            result.append(self._delta(delta))
            return result
        if kind != "tool":
            return []
        key = event["index"]
        slot = self.tools.setdefault(key, {"id": "", "name": "", "args": "", "started": False})
        for source, target in (("id", "id"), ("name", "name")):
            value = event.get(source)
            if value and value != slot[target]:
                if slot["started"]:
                    raise ValueError("upstream changed tool metadata after content_block_start")
                slot[target] += value
                self.retained += len(value.encode("utf-8"))
        args = event.get("args") or ""
        slot["args"] += args
        self.retained += len(args.encode("utf-8"))
        if self.retained > MAX_FRAME:
            raise ValueError("tool parameters exceed 1 MiB")
        if self.active_tool is None:
            self.active_tool = key
        if key != self.active_tool:
            return []
        if not slot["started"]:
            if slot["id"] and slot["name"] and slot["args"]:
                return self._tool_start(slot)
            return []
        return [self._delta({"type": "input_json_delta", "partial_json": args})] if args else []

    def finish(self) -> list[tuple[str, dict]]:
        if not self.reason:
            raise ValueError("upstream ended without finish_reason")
        result: list[tuple[str, dict]] = []
        for slot in self.tools.values():
            value = json.loads(slot["args"] or "{}")
            if not isinstance(value, dict):
                raise ValueError("tool parameters must be a JSON object")
            if not slot["started"]:
                result.extend(self._tool_start(slot))
            result.extend(self._stop())
        result.extend(self._stop())
        if self.index < 0:
            result.extend(self._start("text", {"type": "text", "text": ""}))
            result.extend(self._stop())
        return result
