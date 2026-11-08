"""OpenAI Responses request conversion and incremental output events.

The inference backend speaks Chat Completions. This module keeps Responses
item/call identities and tool namespaces separate from Anthropic translation.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
import threading
import time
import uuid
from typing import Any

MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_TOOL_BYTES = 1024 * 1024


class RequestError(ValueError):
    def __init__(self, message: str, param: str | None = None, *, status=400, code=None):
        super().__init__(message)
        self.param, self.status, self.code = param, status, code


def error_body(message: str, *, param=None, code=None, kind="invalid_request_error") -> dict:
    return {"error": {"message": message, "type": kind, "param": param, "code": code}}


def _object(value: Any, param: str) -> dict:
    if not isinstance(value, dict):
        raise RequestError("Expected an object", param)
    return value


def _string(value: Any, param: str, *, empty=True) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise RequestError("Expected a string" + ("" if empty else " that is not empty"), param)
    return value


def _wire_name(name: str, namespace: str | None) -> str:
    value = f"{namespace}__{name}" if namespace else name
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        return value
    return "tool_" + hashlib.sha256(value.encode()).hexdigest()[:40]


@dataclass(frozen=True)
class Tool:
    name: str
    namespace: str | None = None
    kind: str = "function"

    @property
    def wire_name(self):
        return _wire_name(self.name, self.namespace)


def convert_tools(value: Any) -> tuple[list[dict], dict[str, Tool]]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise RequestError("Expected an array", "tools")
    output, registry = [], {}

    def add(tool, param, namespace=None):
        tool = _object(tool, param)
        kind = tool.get("type")
        name = _string(tool.get("name"), param + ".name", empty=False)
        if kind == "namespace":
            if namespace:
                raise RequestError("Nested tool namespaces are not supported", param)
            children = tool.get("tools")
            if not isinstance(children, list):
                raise RequestError("Expected an array", param + ".tools")
            for i, child in enumerate(children):
                add(child, f"{param}.tools[{i}]", name)
            return
        if kind not in ("function", "custom"):
            raise RequestError(f"Tool type '{kind}' is not supported by this upstream", param + ".type")
        spec = Tool(name, namespace, kind)
        if spec.wire_name in registry:
            raise RequestError("Duplicate or colliding tool name", param + ".name")
        registry[spec.wire_name] = spec
        description = _string(tool.get("description", ""), param + ".description")
        if kind == "custom":
            format_ = _object(tool.get("format") or {"type": "text"}, param + ".format")
            if format_.get("type") not in ("text", "grammar"):
                raise RequestError("Unsupported custom tool format", param + ".format")
            if format_.get("type") == "grammar":
                grammar = _string(format_.get("definition"), param + ".format.definition")
                description += "\nInput grammar (" + str(format_.get("syntax", "lark")) + "):\n" + grammar
            description += '\nPass the complete raw tool input as the JSON string field "input".'
            parameters = {"type": "object", "properties": {"input": {"type": "string"}},
                          "required": ["input"], "additionalProperties": False}
        else:
            parameters = _object(tool.get("parameters") or {"type": "object", "properties": {}}, param + ".parameters")
        function = {"name": spec.wire_name, "description": description, "parameters": parameters}
        if "strict" in tool:
            if not isinstance(tool["strict"], bool):
                raise RequestError("Expected a boolean", param + ".strict")
            function["strict"] = tool["strict"]
        output.append({"type": "function", "function": function})

    for i, tool in enumerate(value):
        add(tool, f"tools[{i}]")
    return output, registry


def _content(value: Any, param: str, *, images=True) -> str | list:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise RequestError("Expected a string or content array", param)
    parts = []
    for i, part in enumerate(value):
        p = f"{param}[{i}]"
        part = _object(part, p)
        kind = part.get("type")
        if kind in ("input_text", "output_text", "summary_text", "reasoning_text"):
            parts.append({"type": "text", "text": _string(part.get("text"), p + ".text")})
        elif kind == "refusal":
            parts.append({"type": "text", "text": _string(part.get("refusal"), p + ".refusal")})
        elif kind == "input_image" and images:
            image = {"url": _string(part.get("image_url"), p + ".image_url", empty=False)}
            if part.get("detail"):
                image["detail"] = part["detail"]
            parts.append({"type": "image_url", "image_url": image})
        else:
            raise RequestError(f"Content type '{kind}' is not supported", p + ".type")
    if all(p["type"] == "text" for p in parts):
        return "\n".join(p["text"] for p in parts)
    return parts


def input_items(value: Any) -> list[dict]:
    if isinstance(value, str):
        return [{"type": "message", "role": "user", "content": value}]
    if not isinstance(value, list):
        raise RequestError("Expected a string or array of input items", "input")
    return deepcopy(value)


def convert_input(items: list, instructions: str | None, registry: dict[str, Tool]) -> list[dict]:
    output = []
    if instructions:
        output.append({"role": "system", "content": instructions})
    pending_reasoning = ""
    for i, item in enumerate(items):
        p = f"input[{i}]"
        item = _object(item, p)
        kind = item.get("type", "message")
        if kind == "message":
            role = item.get("role")
            if role not in ("user", "assistant", "system", "developer"):
                raise RequestError("Unsupported message role", p + ".role")
            message = {"role": "system" if role == "developer" else role,
                       "content": _content(item.get("content", ""), p + ".content", images=role == "user")}
            if role == "assistant" and pending_reasoning:
                message["reasoning_content"] = pending_reasoning
                pending_reasoning = ""
            output.append(message)
        elif kind == "reasoning":
            for field in ("summary", "content"):
                if item.get(field):
                    pending_reasoning += _content(item[field], p + "." + field, images=False)
            # Encrypted OpenAI reasoning cannot be decrypted by the Kimi backend.
        elif kind in ("function_call", "custom_tool_call"):
            name = _string(item.get("name"), p + ".name", empty=False)
            namespace = item.get("namespace")
            if namespace is not None:
                namespace = _string(namespace, p + ".namespace", empty=False)
            spec = Tool(name, namespace, "custom" if kind == "custom_tool_call" else "function")
            registry.setdefault(spec.wire_name, spec)
            arguments = (_string(item.get("arguments"), p + ".arguments") if kind == "function_call"
                         else json.dumps({"input": _string(item.get("input"), p + ".input")}, ensure_ascii=False))
            call = {"id": _string(item.get("call_id"), p + ".call_id", empty=False), "type": "function",
                    "function": {"name": spec.wire_name, "arguments": arguments}}
            if output and output[-1]["role"] == "assistant":
                message = output[-1]
            else:
                message = {"role": "assistant", "content": ""}
                output.append(message)
            if pending_reasoning:
                message["reasoning_content"] = message.get("reasoning_content", "") + pending_reasoning
                pending_reasoning = ""
            message.setdefault("tool_calls", []).append(call)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            output.append({"role": "tool", "tool_call_id": _string(item.get("call_id"), p + ".call_id", empty=False),
                           "content": _content(item.get("output"), p + ".output")})
        else:
            raise RequestError(f"Input item type '{kind}' is not supported", p + ".type")
    # A truncated thinking-only response can be sent back during budget recovery.
    if pending_reasoning:
        output.append({"role": "assistant", "content": "", "reasoning_content": pending_reasoning})
    return output


def prepare_request(body: dict, backend, prior: dict | None = None) -> tuple[dict, list, dict]:
    for key in ("stream", "store", "parallel_tool_calls", "background"):
        if key in body and not isinstance(body[key], bool):
            raise RequestError("Expected a boolean", key)
    if body.get("background"):
        raise RequestError("Background responses are not supported", "background")
    for key in ("conversation", "prompt", "context_management"):
        if body.get(key) is not None:
            raise RequestError(f"'{key}' is not supported; send explicit input history", key)
    if "input" not in body:
        raise RequestError("Missing required parameter: input", "input")
    instructions = body.get("instructions")
    if instructions is not None:
        _string(instructions, "instructions")
    max_tokens = body.get("max_output_tokens")
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1):
        raise RequestError("Expected a positive integer", "max_output_tokens")
    reasoning = _object(body.get("reasoning") if body.get("reasoning") is not None else {}, "reasoning")
    if reasoning.get("summary") not in (None, "auto", "concise", "detailed"):
        raise RequestError("Unsupported reasoning summary", "reasoning.summary")
    if body.get("metadata") is not None:
        metadata = _object(body["metadata"], "metadata")
        if len(metadata) > 16 or any(not isinstance(v, str) or len(k) > 64 or len(v) > 512 for k, v in metadata.items()):
            raise RequestError("metadata supports up to 16 string pairs (64 character keys, 512 character values)", "metadata")
    effort = reasoning.get("effort")
    if effort is not None and effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
        raise RequestError("Unsupported reasoning effort", "reasoning.effort")
    mapped_effort = "low" if effort in ("none", "minimal") else effort
    payload = backend.to_openai_request({"model": body["model"], "messages": [],
                                        "max_tokens": max_tokens, "output_config": {"effort": mapped_effort}})
    backend._enable_upstream_stream(payload)
    tools, registry = convert_tools(body.get("tools"))
    items = deepcopy(prior["items"]) if prior else []
    items.extend(input_items(body["input"]))
    if prior:
        for name, tool in prior["registry"].items():
            registry.setdefault(name, tool)
    payload["messages"] = convert_input(items, instructions, registry)
    if tools:
        payload["tools"] = tools
    choice = body.get("tool_choice", "auto")
    if isinstance(choice, dict):
        if choice.get("type") not in ("function", "custom"):
            raise RequestError("Unsupported tool choice", "tool_choice")
        name = _wire_name(_string(choice.get("name"), "tool_choice.name", empty=False), choice.get("namespace"))
        if name not in {t["function"]["name"] for t in tools}:
            raise RequestError("Chosen tool is not present in tools", "tool_choice.name")
        choice = {"type": "function", "function": {"name": name}}
    elif choice not in ("auto", "required", "none"):
        raise RequestError("Unsupported tool choice", "tool_choice")
    if tools:
        payload["tool_choice"] = choice
    elif choice not in ("auto", "none"):
        raise RequestError("tool_choice requires tools", "tool_choice")
    if "parallel_tool_calls" in body:
        payload["parallel_tool_calls"] = body["parallel_tool_calls"]
    text = _object(body.get("text") if body.get("text") is not None else {}, "text")
    format_ = _object(text.get("format") if text.get("format") is not None else {"type": "text"}, "text.format")
    if format_.get("type") == "json_schema":
        payload["response_format"] = {"type": "json_schema", "json_schema": {
            "name": _string(format_.get("name"), "text.format.name", empty=False),
            "schema": _object(format_.get("schema"), "text.format.schema"), "strict": format_.get("strict", True)}}
    elif format_.get("type") == "json_object":
        payload["response_format"] = {"type": "json_object"}
    elif format_.get("type") != "text":
        raise RequestError("Unsupported output format", "text.format.type")
    backend._maybe_break_tool_loop(payload)
    return payload, items, registry


class ResponseStore:
    """Process-local bounded TTL history; never writes prompts or credentials to disk."""
    def __init__(self, *, max_bytes=32 * 1024 * 1024, max_entries=128, ttl=3600):
        self.max_bytes, self.max_entries, self.ttl = max_bytes, max_entries, ttl
        self.entries = OrderedDict()
        self.size = 0
        self.lock = threading.Lock()

    def _purge(self):
        for key, (until, size, _) in list(self.entries.items()):
            if until <= time.monotonic():
                self.entries.pop(key)
                self.size -= size

    def put(self, response, items, registry):
        value = {"response": deepcopy(response), "items": deepcopy(items + response["output"]), "registry": dict(registry)}
        size = len(json.dumps({"response": response, "items": value["items"]}, ensure_ascii=False).encode())
        # Tool names/descriptions are bounded by the request body; count registry keys too.
        size += sum(len(k.encode()) + len(repr(v).encode()) for k, v in registry.items())
        if size > self.max_bytes:
            raise ValueError("Stored response history exceeds the process memory limit; use store=false")
        with self.lock:
            self._purge()
            previous = self.entries.pop(response["id"], None)
            if previous:
                self.size -= previous[1]
            while self.entries and (self.size + size > self.max_bytes or len(self.entries) >= self.max_entries):
                _, (_, removed, _) = self.entries.popitem(last=False)
                self.size -= removed
            self.entries[response["id"]] = (time.monotonic() + self.ttl, size, value)
            self.size += size

    def get(self, response_id):
        with self.lock:
            self._purge()
            if response_id not in self.entries:
                raise RequestError("Response not found or expired", "previous_response_id", status=404, code="response_not_found")
            self.entries.move_to_end(response_id)
            return deepcopy(self.entries[response_id][2])

    def delete(self, response_id):
        with self.lock:
            self._purge()
            value = self.entries.pop(response_id, None)
            if value is None:
                raise RequestError("Response not found or expired", status=404, code="response_not_found")
            self.size -= value[1]


class CustomInputDecoder:
    """Decode the JSON wrapper's string incrementally, including split escapes."""
    def __init__(self):
        self.prefix, self.pending = "", ""
        self.started = self.ended = False
        self.high_surrogate = None

    def feed(self, fragment):
        if self.ended:
            return ""
        if not self.started:
            self.prefix += fragment
            match = re.match(r'^\s*\{\s*"input"\s*:\s*"', self.prefix)
            if match is None:
                if len(self.prefix) > 256:
                    raise ValueError('Custom tool arguments must start with {"input":"..."}')
                return ""
            self.started = True
            fragment, self.prefix = self.prefix[match.end():], ""
        data, self.pending = self.pending + fragment, ""
        output, i = [], 0
        while i < len(data):
            c = data[i]
            if c == '"':
                self.ended = True
                break
            if c == "\\":
                if i + 1 >= len(data):
                    self.pending = data[i:]
                    break
                escape = data[i + 1]
                if escape == "u":
                    if i + 6 > len(data):
                        self.pending = data[i:]
                        break
                    c = chr(int(data[i + 2:i + 6], 16))
                    i += 6
                else:
                    table = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
                    if escape not in table:
                        raise ValueError("Invalid custom tool JSON escape")
                    c, i = table[escape], i + 2
            else:
                if ord(c) < 32:
                    raise ValueError("Unescaped control character in custom tool input")
                i += 1
            if self.high_surrogate is not None:
                if not 0xDC00 <= ord(c) <= 0xDFFF:
                    raise ValueError("Invalid Unicode surrogate in custom tool input")
                c = chr(0x10000 + ((self.high_surrogate - 0xD800) << 10) + ord(c) - 0xDC00)
                self.high_surrogate = None
            elif 0xD800 <= ord(c) <= 0xDBFF:
                self.high_surrogate = ord(c)
                continue
            elif 0xDC00 <= ord(c) <= 0xDFFF:
                raise ValueError("Invalid Unicode surrogate in custom tool input")
            output.append(c)
        return "".join(output)


class ResponseEvents:
    """Track independent output items and generate ordered Responses SSE events."""
    def __init__(self, body: dict, registry: dict[str, Tool]):
        self.registry = registry
        self.sequence = 0
        self.active = None
        self.slots = []
        self.tools = {}
        self.reason = None
        self.retained = 0
        self.usage = {}
        self.response = {
            "id": "resp_" + uuid.uuid4().hex, "object": "response", "created_at": int(time.time()),
            "status": "in_progress", "error": None, "incomplete_details": None, "output": [],
            "model": body["model"], "instructions": body.get("instructions"),
            "max_output_tokens": body.get("max_output_tokens"), "metadata": body.get("metadata") or {},
            "parallel_tool_calls": body.get("parallel_tool_calls", True),
            "previous_response_id": body.get("previous_response_id"), "reasoning": body.get("reasoning"),
            "store": body.get("store", True), "background": False, "temperature": None, "top_p": None,
            "text": body.get("text") or {"format": {"type": "text"}}, "tool_choice": body.get("tool_choice", "auto"),
            "tools": body.get("tools") or [], "truncation": "disabled", "usage": None,
        }

    def event(self, kind, **values):
        value = {"type": kind, "sequence_number": self.sequence, **deepcopy(values)}
        self.sequence += 1
        return value

    def start(self):
        return [self.event("response.created", response=self.response),
                self.event("response.in_progress", response=self.response)]

    def _new(self, item):
        slot = {"item": item, "index": len(self.slots), "parts": [], "closed": False}
        self.slots.append(slot)
        self.response["output"].append(item)
        return slot

    def _indices(self, slot):
        return {"item_id": slot["item"]["id"], "output_index": slot["index"]}

    def _close(self, slot, *, incomplete=False):
        if slot["closed"]:
            return []
        item, result = slot["item"], []
        indices = self._indices(slot)
        text = "".join(slot["parts"])
        if item["type"] == "reasoning":
            part = {"type": "summary_text", "text": text}
            item["summary"] = [part]
            result += [self.event("response.reasoning_summary_text.done", **indices, summary_index=0, text=text),
                       self.event("response.reasoning_summary_part.done", **indices, summary_index=0, part=part)]
        elif item["type"] == "message":
            part = {"type": "output_text", "text": text, "annotations": [], "logprobs": []}
            item["content"] = [part]
            result += [self.event("response.output_text.done", **indices, content_index=0, text=text, logprobs=[]),
                       self.event("response.content_part.done", **indices, content_index=0, part=part)]
        else:
            field = "input" if item["type"] == "custom_tool_call" else "arguments"
            item[field] = text
            event = "response.custom_tool_call_input.done" if field == "input" else "response.function_call_arguments.done"
            result.append(self.event(event, **indices, **{field: text}))
        item["status"] = "incomplete" if incomplete else "completed"
        slot["closed"] = True
        result.append(self.event("response.output_item.done", output_index=slot["index"], item=item))
        return result

    def _close_active(self):
        if self.active is None:
            return []
        result = self._close(self.active)
        self.active = None
        return result

    def _account(self, text):
        self.retained += len(text.encode("utf-8"))
        if self.retained > MAX_RESPONSE_BYTES:
            raise ValueError("Response output exceeds 16 MiB")

    def feed(self, value: dict) -> list[dict]:
        kind = value["type"]
        if kind == "usage":
            self.usage = value["usage"]
            return []
        if kind == "finish":
            self.reason = value["reason"]
            return []
        if kind in ("thinking", "text"):
            text = value["text"]
            self._account(text)
            item_kind = "reasoning" if kind == "thinking" else "message"
            result = []
            if self.active is None or self.active["item"]["type"] != item_kind:
                result.extend(self._close_active())
                item = ({"id": "rs_" + uuid.uuid4().hex, "type": "reasoning", "summary": [], "status": "in_progress"}
                        if kind == "thinking" else {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
                                                   "content": [], "status": "in_progress"})
                self.active = self._new(item)
                result.append(self.event("response.output_item.added", output_index=self.active["index"], item=item))
                indices = self._indices(self.active)
                if kind == "thinking":
                    result.append(self.event("response.reasoning_summary_part.added", **indices, summary_index=0,
                                             part={"type": "summary_text", "text": ""}))
                else:
                    result.append(self.event("response.content_part.added", **indices, content_index=0,
                                             part={"type": "output_text", "text": "", "annotations": [], "logprobs": []}))
            self.active["parts"].append(text)
            indices = self._indices(self.active)
            if kind == "thinking":
                result.append(self.event("response.reasoning_summary_text.delta", **indices, summary_index=0, delta=text))
            else:
                result.append(self.event("response.output_text.delta", **indices, content_index=0, delta=text, logprobs=[]))
            return result
        if kind != "tool":
            return []
        result = self._close_active()
        tool = self.tools.setdefault(value["index"], {"id": "", "name": "", "raw": "", "slot": None})
        for field in ("id", "name"):
            fragment = value.get(field)
            if fragment and fragment != tool[field]:
                if tool["slot"] is not None:
                    raise ValueError("Upstream changed tool identity after output_item.added")
                tool[field] += fragment
        args = value.get("args") or ""
        self._account(args)
        tool["raw"] += args
        if len(tool["raw"].encode()) > MAX_TOOL_BYTES:
            raise ValueError("Tool input exceeds 1 MiB")
        if tool["slot"] is None:
            if not tool["id"] or not tool["name"] or not tool["raw"]:
                return result
            spec = self.registry.get(tool["name"])
            if spec is None:
                raise ValueError("Upstream called an unknown tool: " + tool["name"])
            item = {"type": "custom_tool_call" if spec.kind == "custom" else "function_call",
                    "id": "fc_" + uuid.uuid4().hex, "call_id": tool["id"], "name": spec.name,
                    "status": "in_progress", "input" if spec.kind == "custom" else "arguments": ""}
            if spec.namespace:
                item["namespace"] = spec.namespace
            tool["slot"] = self._new(item)
            if spec.kind == "custom":
                tool["decoder"] = CustomInputDecoder()
            result.append(self.event("response.output_item.added", output_index=tool["slot"]["index"], item=item))
            args = tool["raw"]
        slot = tool["slot"]
        if "decoder" in tool:
            args = tool["decoder"].feed(args)
        if args:
            slot["parts"].append(args)
            event = ("response.custom_tool_call_input.delta" if "decoder" in tool
                     else "response.function_call_arguments.delta")
            result.append(self.event(event, **self._indices(slot), delta=args))
        return result

    def finish(self):
        if not self.reason:
            raise ValueError("Upstream response ended without finish_reason")
        incomplete = self.reason in ("length", "content_filter")
        for tool in self.tools.values():
            if tool["slot"] is None:
                raise ValueError("Upstream tool call is missing id, name or arguments")
            if not incomplete:
                parsed = json.loads(tool["raw"])
                if not isinstance(parsed, dict):
                    raise ValueError("Tool arguments must be an object")
                if "decoder" in tool:
                    if set(parsed) != {"input"} or not isinstance(parsed["input"], str):
                        raise ValueError("Custom tool arguments must contain one input string")
                    if parsed["input"] != "".join(tool["slot"]["parts"]):
                        raise ValueError("Custom tool input was not decoded completely")
        result = []
        for slot in self.slots:
            result.extend(self._close(slot, incomplete=incomplete))
        input_tokens, output_tokens = self.usage.get("prompt_tokens", 0), self.usage.get("completion_tokens", 0)
        self.response["usage"] = {
            "input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens,
            "input_tokens_details": {"cached_tokens": (self.usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)},
            "output_tokens_details": {"reasoning_tokens": (self.usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)},
        }
        self.response["status"] = "incomplete" if incomplete else "completed"
        if incomplete:
            self.response["incomplete_details"] = {"reason": "max_output_tokens" if self.reason == "length" else "content_filter"}
        result.append(self.event("response.incomplete" if incomplete else "response.completed", response=self.response))
        return result

    def fail(self, message, code="server_error"):
        # Preserve already delivered partial output without issuing done events.
        for slot in self.slots:
            if slot["closed"]:
                continue
            item, text = slot["item"], "".join(slot["parts"])
            if item["type"] == "reasoning":
                item["summary"] = [{"type": "summary_text", "text": text}]
            elif item["type"] == "message":
                item["content"] = [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}]
            else:
                item["input" if item["type"] == "custom_tool_call" else "arguments"] = text
            item["status"] = "incomplete"
        self.response["status"] = "failed"
        self.response["error"] = {"code": code, "message": message}
        return self.event("response.failed", response=self.response)
