"""
nvplay_bridge.py — Anthropic Messages API 适配层

三种上游模式：

  openai  : 任意 OpenAI Chat Completions 兼容服务
            （NVIDIA NIM / Kimi /v1 / 其他兼容服务）

  nvcf    : NVIDIA Build 的 NVCF predict 代理
            （Cookie + Nv-Captcha-Token 鉴权，SSE）

  browser : 通过 CDP 驱动已登录/匿名的 Brave Playground 页面
            （页面现场签 nv-captcha-token，我们在 fetch 层替换 body）
            需要 browser_upstream.py 与 websocket-client

运行（browser 模式，推荐）：

    pip install fastapi uvicorn httpx websocket-client

    # 1. 完全退出 Brave，然后用调试端口 + 真实 profile 启动
    #    （见 research/README.md）
    # 2. 启动适配层
    export UPSTREAM_KIND=browser
    export UPSTREAM_MODEL="moonshotai/kimi-k3.1-pro"
    export BROWSER_CDP_PORT=9222
    uvicorn nvplay_bridge:app --host 127.0.0.1 --port 8083

运行（NVCF 模式）：

    pip install fastapi uvicorn httpx

    export UPSTREAM_KIND=nvcf
    export NVCF_URL="https://buildapi.nvcf.nvidia.com/v2/predict/models/q6gjvmznx2/kimi-k3.1-pro"
    export NV_COOKIE_FILE="./.nvcf_cookies.txt"     # 抓包里的整行 Cookie
    export NV_CAPTCHA_FILE="./.nvcf_captcha.txt"    # Nv-Captcha-Token 的值
    uvicorn nvplay_bridge:app --host 127.0.0.1 --port 8083

运行（OpenAI 兼容模式）：

    export UPSTREAM_KIND=openai
    export UPSTREAM_BASE_URL="https://integrate.api.nvidia.com/v1"
    export UPSTREAM_API_KEY="nvapi-..."
    export UPSTREAM_MODEL="moonshotai/kimi-k3.1-pro"

仍未覆盖的部分：
    - 没有客户端鉴权（默认只监听 127.0.0.1）
    - count_tokens 是字符数估算，不是真 tokenizer
    - 没有限流、断线重连；NVCF 会话是单账号单进程
"""

from __future__ import annotations

from contextlib import aclosing, asynccontextmanager
from contextvars import ContextVar
from stream_support import AnthropicBlocks, DeferredEvents

import asyncio
import hashlib
import json
import os
import random
import sys
import threading
import time
import uuid
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

_HERE = os.path.dirname(os.path.abspath(__file__))

UPSTREAM_KIND = os.getenv("UPSTREAM_KIND", "openai").strip().lower()
if UPSTREAM_KIND not in {"openai", "nvcf", "browser"}:
    raise RuntimeError("UPSTREAM_KIND must be 'openai', 'nvcf' or 'browser'")

# 浏览器桥（CDP）模式
BROWSER_CDP_PORT = int(os.getenv("BROWSER_CDP_PORT", "9222"))
BROWSER_TARGET_HINT = os.getenv("BROWSER_TARGET_HINT", "kimi-k3_1-pro/playground")
# 本桥默认将输出限制为 128K；这不是 Kimi 官方 API 的能力上限。
BROWSER_MAX_TOKENS = int(os.getenv("BROWSER_MAX_TOKENS", "131072"))
BROWSER_FIXED_MAX_TOKENS = int(os.getenv("BROWSER_FIXED_MAX_TOKENS", "0"))
if not 0 <= BROWSER_FIXED_MAX_TOKENS <= BROWSER_MAX_TOKENS:
    raise RuntimeError("BROWSER_FIXED_MAX_TOKENS must be 0 (disabled) or at most BROWSER_MAX_TOKENS")
BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT", "3600"))
BROWSER_HEARTBEAT_SEC = float(os.getenv("BROWSER_HEARTBEAT_SEC", "10"))
BROWSER_POOL_SIZE = int(os.getenv("BROWSER_POOL_SIZE", "5"))
BROWSER_DEBUG_LOG = os.getenv("BROWSER_DEBUG_LOG", "")
BROWSER_POOL_TIMEOUT_RETRIES = max(0, int(os.getenv("BROWSER_POOL_TIMEOUT_RETRIES", "2")))
BROWSER_POOL_TIMEOUT_BACKOFF = max(0.0, float(os.getenv("BROWSER_POOL_TIMEOUT_BACKOFF", "5")))
BROWSER_HTTP_5XX_RETRIES = max(0, int(os.getenv("BROWSER_HTTP_5XX_RETRIES", "3")))
BROWSER_HTTP_5XX_BACKOFF = max(0.0, float(os.getenv("BROWSER_HTTP_5XX_BACKOFF", "5")))
BROWSER_FETCH_RETRIES = max(0, int(os.getenv("BROWSER_FETCH_RETRIES", "1")))
BROWSER_FETCH_BACKOFF = max(0.0, float(os.getenv("BROWSER_FETCH_BACKOFF", "2")))
BROWSER_STREAM_RETRIES = max(0, int(os.getenv("BROWSER_STREAM_RETRIES", "2")))
BROWSER_STREAM_BACKOFF = max(0.0, float(os.getenv("BROWSER_STREAM_BACKOFF", "5")))
BROWSER_MESSAGES_DEFER_OUTPUT = os.getenv("BROWSER_MESSAGES_DEFER_OUTPUT", "1") == "1"
PLACEHOLDER_RETRY = int(os.getenv("PLACEHOLDER_RETRY", "1"))
LOOP_BREAKER = int(os.getenv("LOOP_BREAKER", "0"))
HISTORY_REASONING = os.getenv("HISTORY_REASONING", "preserve").strip().lower()
if HISTORY_REASONING not in {"preserve", "omit"}:
    raise RuntimeError("HISTORY_REASONING must be 'preserve' or 'omit'")
BROWSER_DEBUG_MESSAGES = os.getenv("BROWSER_DEBUG_MESSAGES", "") == "1"
_request_id: ContextVar[str | None] = ContextVar("bridge_request_id", default=None)
_INSTANCE_ID = uuid.uuid4().hex[:12]
_PROCESS_STARTED = time.monotonic()


def _dbg(event: str, **data: Any) -> None:
    if not BROWSER_DEBUG_LOG:
        return
    try:
        request_id = _request_id.get()
        record = {"ts": time.time(), "event": event, "instance_id": _INSTANCE_ID, "pid": os.getpid(),
                  **({"request_id": request_id} if request_id else {}), **data}
        with open(BROWSER_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


class RequestDiagnosticsMiddleware:
    """Keep request IDs across streaming tasks without buffering response bodies."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or scope.get("path") not in ("/v1/messages", "/v1/responses")):
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex[:24]
        token = _request_id.set(request_id)
        started = time.monotonic()
        status = None
        complete = disconnected = False

        async def receive_traced():
            nonlocal disconnected
            message = await receive()
            # ASGI servers also return http.disconnect after a completed body.
            # That normal response teardown is not a cancelled generation.
            if message["type"] == "http.disconnect" and not complete:
                disconnected = True
            return message

        async def send_traced(message):
            nonlocal status, complete
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": list(message.get("headers", [])) + [
                    (b"request-id", request_id.encode()), (b"x-request-id", request_id.encode())]}
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        try:
            await self.app(scope, receive_traced, send_traced)
        finally:
            _dbg("http_complete", path=scope["path"], status=status, body_complete=complete,
                 client_disconnected=disconnected, sec=round(time.monotonic() - started, 3))
            _request_id.reset(token)

# OpenAI 兼容模式
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
UPSTREAM_KEY = os.getenv("UPSTREAM_API_KEY", "")
UPSTREAM_MODEL = os.getenv("UPSTREAM_MODEL", "moonshotai/kimi-k3.1-pro")
OPENAI_MAX_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "131072"))

# 请求里的 model 只作为别名白名单；真正发给上游的始终是 UPSTREAM_MODEL。
ALLOWED_MODELS = {
    item.strip()
    for item in os.getenv(
        "ALLOWED_MODELS",
        "kimi-k3.1-pro,kimi-k3_1-pro,moonshotai/kimi-k3.1-pro,moonshotai/kimi-k3_1-pro",
    ).split(",")
    if item.strip()
}
ALLOWED_MODELS.add(UPSTREAM_MODEL)
_ALLOWED_MODELS_LOWER = {item.lower() for item in ALLOWED_MODELS}


class ModelNotSupported(ValueError):
    pass


def validate_model(model: Any) -> str:
    requested = str(model).strip() if model else UPSTREAM_MODEL
    if requested.lower() not in _ALLOWED_MODELS_LOWER:
        raise ModelNotSupported(requested)
    return requested

# NVCF 模式
NVCF_URL = os.getenv("NVCF_URL", "").strip()
NVCF_MAX_TOKENS = int(os.getenv("NVCF_MAX_TOKENS", "131072"))
NV_COOKIE_FILE = os.getenv("NV_COOKIE_FILE", os.path.join(_HERE, ".nvcf_cookies.txt"))
NV_CAPTCHA_FILE = os.getenv("NV_CAPTCHA_FILE", os.path.join(_HERE, ".nvcf_captcha.txt"))
NV_ORIGIN = os.getenv("NV_ORIGIN", "https://build.nvidia.com")
NV_REFERER = os.getenv("NV_REFERER", "https://build.nvidia.com/")
NV_UA = os.getenv(
    "NV_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
)

PASS_SAMPLING = os.getenv("PASS_SAMPLING", "0") == "1"


@asynccontextmanager
async def _lifespan(app):
    global _browser
    _dbg("bridge_started", upstream_kind=UPSTREAM_KIND, model=UPSTREAM_MODEL)
    try:
        yield
    finally:
        browser, _browser = _browser, None
        try:
            if browser is not None:
                await asyncio.to_thread(browser.close)
        finally:
            _dbg("bridge_stopped")


app = FastAPI(
    title="nvplay-bridge",
    version="0.3.0",
    description="Anthropic Messages and OpenAI Responses bridge for Kimi K3.1 Pro via NVIDIA Build Playground",
    lifespan=_lifespan,
)
app.add_middleware(RequestDiagnosticsMiddleware)


class DisconnectStreamingResponse(StreamingResponse):
    """Always observe disconnects, including ASGI 2.4 while upstream is idle."""
    async def __call__(self, scope, receive, send) -> None:
        sending = asyncio.create_task(self.stream_response(send))
        disconnected = asyncio.create_task(self.listen_for_disconnect(receive))
        try:
            completed, _ = await asyncio.wait((sending, disconnected), return_when=asyncio.FIRST_COMPLETED)
            if sending in completed:
                await sending
            else:
                await disconnected
        except OSError:
            pass
        finally:
            sending.cancel()
            disconnected.cancel()
            await asyncio.gather(sending, disconnected, return_exceptions=True)
            await self.body_iterator.aclose()
        if self.background is not None:
            await self.background()


async def _disconnected(request: Request) -> None:
    while not await request.is_disconnected():
        await asyncio.sleep(0.1)


class ConfigError(RuntimeError):
    pass


class UpstreamError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# 会话文件（NVCF）
# --------------------------------------------------------------------------- #

def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def load_cookie() -> str:
    env = os.getenv("NV_COOKIE", "").strip()
    return env or _read_text(NV_COOKIE_FILE)


def load_captcha() -> str:
    env = os.getenv("NV_CAPTCHA_TOKEN", "").strip()
    return env or _read_text(NV_CAPTCHA_FILE)


def _write_text(path: str, value: str) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value)
    except OSError:
        pass


def merge_set_cookie(cookie_header: str, set_cookies: list[str]) -> str:
    """把响应里的 Set-Cookie 合并进请求 Cookie 串。"""
    jar: dict[str, str] = {}
    for part in cookie_header.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            jar[key.strip()] = value.strip()
    for raw in set_cookies:
        first = raw.split(";", 1)[0].strip()
        if "=" in first:
            key, value = first.split("=", 1)
            jar[key.strip()] = value.strip()
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def _refresh_cookie(response: httpx.Response) -> None:
    if os.getenv("NV_COOKIE"):
        return  # 显式覆盖时不回写
    set_cookies = response.headers.get_list("set-cookie")
    if not set_cookies:
        return
    current = load_cookie()
    merged = merge_set_cookie(current, set_cookies)
    if merged and merged != current:
        _write_text(NV_COOKIE_FILE, merged)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _loads(raw: str | None) -> Any:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        raise UpstreamError(502, "upstream tool parameters are not valid JSON") from None
    if not isinstance(value, dict):
        raise UpstreamError(502, "upstream tool parameters must be a JSON object")
    return value


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


def _tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    else:
        text = json.dumps(content, ensure_ascii=False)
    if block.get("is_error"):
        text = "Error: " + text
    return text


def _image_part(block: dict[str, Any]) -> dict[str, Any] | None:
    source = block.get("source") or {}
    if source.get("type") == "base64":
        mime = source.get("media_type", "image/png")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{source.get('data', '')}"},
        }
    if source.get("type") == "url":
        return {"type": "image_url", "image_url": {"url": source.get("url", "")}}
    return None


def _normalize_effort(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"low", "medium", "high", "xhigh", "max"}:
        return normalized
    return None


def _explicit_effort(body: dict[str, Any]) -> str | None:
    """Anthropic adaptive effort：output_config.effort 优先，其次 effort/header override。"""
    output_config = body.get("output_config")
    if isinstance(output_config, dict):
        effort = _normalize_effort(output_config.get("effort"))
        if effort:
            return effort
    for key in ("effort", "reasoning_effort", "_effort_override"):
        effort = _normalize_effort(body.get(key))
        if effort:
            return effort
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "adaptive":
        effort = _normalize_effort(thinking.get("effort"))
        if effort:
            return effort
    return None


def _upstream_effort(effort: str) -> str:
    # Playground/Kimi 的 reasoning_effort 只有 low/medium/high/max；xhigh 归一到 max。
    return "max" if effort == "xhigh" else effort


def _effort_from_thinking(thinking: Any, allow_medium: bool) -> str | None:
    if not isinstance(thinking, dict) or thinking.get("type") != "enabled":
        return None
    budget = int(thinking.get("budget_tokens") or 0)
    if budget <= 2048:
        return "low"
    if allow_medium and budget <= 8192:
        return "medium"
    if budget <= 16384:
        return "high"
    return "max"


def _stop_reason(finish_reason: str | None, has_tools: bool) -> str:
    if finish_reason == "length":
        return "max_tokens"
    if has_tools:
        return "tool_use"
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "end_turn",
    }.get(finish_reason or "", "end_turn")


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def error_body(message: str, err_type: str = "api_error") -> dict[str, Any]:
    return {"type": "error", "error": {"type": err_type, "message": message}}


# --------------------------------------------------------------------------- #
# 请求转换：Anthropic -> OpenAI
# --------------------------------------------------------------------------- #

def _log_tool_call(tool_id: str, name: str, arguments: dict[str, Any]) -> None:
    if not BROWSER_DEBUG_LOG:
        return
    # Compare with CC history without recording source code or shell commands.
    canonical = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    anomaly = None
    if (name == "Edit" and isinstance(arguments.get("old_string"), str)
            and arguments["old_string"] == arguments.get("new_string")):
        anomaly = "identical_edit"
    _dbg("tool_call", tool_id=tool_id, name=name, argument_chars=len(canonical),
         arguments_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(), anomaly=anomaly)


def _log_recent_tool_results(body: dict[str, Any]) -> None:
    if not BROWSER_DEBUG_LOG:
        return
    messages = body.get("messages") or []
    start = len(messages) - 1
    while start >= 0 and messages[start].get("role") != "assistant":
        start -= 1
    content = messages[start].get("content") if start >= 0 else []
    names = {block.get("id"): block.get("name") for block in content or []
             if isinstance(block, dict) and block.get("type") == "tool_use"}
    for message in messages[start + 1:]:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            is_error = bool(block.get("is_error"))
            error_kind = None
            if is_error:
                text = _text_of(block.get("content"))[:512].lower()
                error_kind = next((kind for marker, kind in (
                    ("old_string and new_string are exactly the same", "identical_edit"),
                    ("string to replace not found", "edit_string_not_found"),
                    ("file does not exist", "file_not_found"),
                    ("file has not been read", "file_not_read"),
                    ("matches of the string to replace", "ambiguous_edit"),
                    ("exit code", "command_exit"),
                ) if marker in text), "other")
            tool_id = block.get("tool_use_id")
            _dbg("tool_result", tool_id=tool_id, name=names.get(tool_id),
                 is_error=is_error, error_kind=error_kind)


def _message_shape(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    shapes: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            content_shape: Any = {"text_len": len(content)}
        elif isinstance(content, list):
            content_shape = {
                "blocks": [b.get("type") for b in content if isinstance(b, dict)]
            }
        else:
            content_shape = None
        shapes.append(
            {
                "role": message.get("role"),
                "content": content_shape,
                "tool_calls": [
                    {"id": call.get("id"), "name": (call.get("function") or {}).get("name")}
                    for call in (message.get("tool_calls") or [])
                ],
                "tool_call_id": message.get("tool_call_id"),
                "reasoning_len": len(message.get("reasoning_content") or ""),
            }
        )
    return shapes


def _convert_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # Also accept legacy histories whose upstream IDs were reused across turns.
    tool_id_pools: dict[str, list[str]] = {}

    system = body.get("system")
    if system:
        out.append({"role": "system", "content": _text_of(system)})

    for msg in body.get("messages") or []:
        role = msg.get("role")
        content = msg.get("content")

        if isinstance(content, str):
            if role == "user" and out and out[-1].get("role") == "user" and out[-1].get("content") == content:
                continue
            out.append({"role": role, "content": content})
            continue

        if role == "assistant":
            texts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in content or []:
                btype = block.get("type")
                if btype == "text":
                    texts.append(block.get("text", ""))
                elif btype == "thinking":
                    reasoning_parts.append(block.get("thinking", ""))
                elif btype == "tool_use":
                    old_id = block.get("id") or f"call_{uuid.uuid4().hex[:24]}"
                    new_id = f"call_{uuid.uuid4().hex[:24]}"
                    tool_id_pools.setdefault(old_id, []).append(new_id)
                    tool_calls.append(
                        {
                            "id": new_id,
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                            },
                        }
                    )
                # redacted_thinking 是加密内容，上游无法使用
            item: dict[str, Any] = {"role": "assistant", "content": "".join(texts)}
            if not item["content"].strip() and not tool_calls:
                # thinking-only 的空回合没有可执行内容，留在上下文里会污染模型判断。
                continue
            if item["content"].strip() == "[Tool use interrupted]" and not tool_calls:
                # CC 恢复时留下的占位文本，不应再进入上游上下文。
                continue
            if reasoning_parts:
                # 上游是 OpenAI 方言：把上一轮 thinking 作为 reasoning_content 回传，
                # 让模型延续思考；signature 上游不认识，不传。
                item["reasoning_content"] = "".join(reasoning_parts)
            if tool_calls:
                item["tool_calls"] = tool_calls
            out.append(item)
            continue

        pending: list[dict[str, Any]] = []
        for block in content or []:
            btype = block.get("type")
            if btype == "tool_result":
                if pending:
                    out.append({"role": "user", "content": pending})
                    pending = []
                old_tool_id = block.get("tool_use_id", "")
                pool = tool_id_pools.get(old_tool_id)
                tool_call_id = pool.pop(0) if pool else old_tool_id
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": _tool_result_text(block),
                    }
                )
            elif btype == "text":
                pending.append({"type": "text", "text": block.get("text", "")})
            elif btype == "image":
                part = _image_part(block)
                if part:
                    pending.append(part)
        if pending:
            out.append({"role": "user", "content": pending})

    return out


def _convert_tools(tools: Any) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in (tools or [])
    ]


def _convert_tool_choice(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return None
    ctype = choice.get("type")
    if ctype == "auto":
        return "auto"
    if ctype == "any":
        return "required"
    if ctype == "none":
        return "none"
    if ctype == "tool":
        return {"type": "function", "function": {"name": choice.get("name", "")}}
    return None


def to_openai_request(body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": UPSTREAM_MODEL,
        "messages": _convert_messages(body),
        "stream": False,
    }

    max_tokens = body.get("max_tokens")
    if UPSTREAM_KIND == "nvcf":
        limit = NVCF_MAX_TOKENS
    elif UPSTREAM_KIND == "browser":
        limit = BROWSER_MAX_TOKENS
    else:
        limit = OPENAI_MAX_TOKENS
    payload["max_tokens"] = min(int(max_tokens), limit) if max_tokens else limit
    if UPSTREAM_KIND == "browser" and BROWSER_FIXED_MAX_TOKENS:
        if payload["max_tokens"] != BROWSER_FIXED_MAX_TOKENS:
            _dbg("browser_output_limit_override", requested_max_tokens=max_tokens,
                 effective_max_tokens=BROWSER_FIXED_MAX_TOKENS)
        payload["max_tokens"] = BROWSER_FIXED_MAX_TOKENS
    if body.get("seed") is not None:
        if type(body["seed"]) is not int:
            raise ValueError("seed must be an integer")
        payload["seed"] = body["seed"]

    if PASS_SAMPLING:
        if body.get("temperature") is not None:
            payload["temperature"] = body["temperature"]
        if body.get("top_p") is not None:
            payload["top_p"] = body["top_p"]

    if body.get("stop_sequences"):
        payload["stop"] = body["stop_sequences"]
    if body.get("tools"):
        payload["tools"] = _convert_tools(body["tools"])
        choice = _convert_tool_choice(body.get("tool_choice"))
        if choice is not None:
            payload["tool_choice"] = choice
        if isinstance(body.get("tool_choice"), dict) and body["tool_choice"].get("disable_parallel_tool_use"):
            payload["parallel_tool_calls"] = False

    requested_effort = _explicit_effort(body)
    effort = requested_effort or _effort_from_thinking(body.get("thinking"), allow_medium=(UPSTREAM_KIND in {"nvcf", "browser"}))
    if effort:
        payload["reasoning_effort"] = _upstream_effort(effort)

    return payload


def _tool_loop_key(call: dict[str, Any]) -> tuple[Any, Any]:
    function = call.get("function") or {}
    name = function.get("name")
    raw_args = function.get("arguments")
    try:
        args = json.loads(raw_args or "{}")
    except (TypeError, json.JSONDecodeError):
        args = {}
    if name == "Bash":
        return name, args.get("command") or raw_args
    return name, raw_args


def _maybe_break_tool_loop(payload: dict[str, Any]) -> None:
    """Optional history-only reminder; never infer executions from HTTP retries.

    Compacted histories cannot prove repetition. Avoid shared session counters:
    clients need not provide a stable session ID and often share prompt prefixes.
    """
    if not LOOP_BREAKER:
        return
    messages = payload.get("messages") or []
    calls: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            # Includes structured text/image input and new development stages.
            calls.clear()
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                calls.append({"key": _tool_loop_key(call), "id": call.get("id"), "result": None})
        elif role == "tool":
            for call in reversed(calls):
                if call["id"] == message.get("tool_call_id") and call["result"] is None:
                    call["result"] = message.get("content")
                    break
    if len(calls) < 3 or messages[-1].get("role") != "tool":
        return
    tail = calls[-3:]
    if not all(call["key"] == tail[0]["key"] and isinstance(call["result"], str)
               and call["result"] == tail[0]["result"] for call in tail):
        return
    result = tail[0]["result"].lstrip().lower()
    if result.startswith(("error:", "<tool_use_error>")):
        return
    messages.append(
        {
            "role": "user",
            "content": (
                "[工具调用提醒] 最近三次相同参数的工具调用返回了相同内容。"
                "请检查已有结果是否足够；如需重试，请先判断原因或调整方法，并继续完成用户任务。"
            ),
        }
    )
    _dbg("tool_loop_reminder", tool=tail[0]["key"][0], count=3)


def _enable_upstream_stream(payload: dict[str, Any]) -> None:
    # Both downstream protocols use the same upstream streaming dialect.
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}
    if UPSTREAM_KIND in {"nvcf", "browser"}:
        payload["stream_options"]["continuous_usage_stats"] = True
        payload.pop("temperature", None)
        payload.pop("top_p", None)


def build_payload(body: dict[str, Any]) -> dict[str, Any]:
    requested_effort = _explicit_effort(body)
    _dbg(
        "request",
        requested_model=body.get("model"),
        upstream_model=UPSTREAM_MODEL,
        thinking=body.get("thinking"),
        effort=body.get("effort"),
        output_config=body.get("output_config"),
        explicit_effort=requested_effort,
        max_tokens=body.get("max_tokens"),
        stream=body.get("stream"),
    )
    payload = to_openai_request(body)
    # 上游统一走流式：NVCF 只稳定支持 SSE；非流式在本地聚合。
    _enable_upstream_stream(payload)
    # 不在 CC 未传 thinking/effort 时强行设置 reasoning_effort：
    # 上游没有强制预算要求，只有客户端显式传了才映射。
    _dbg(
        "payload",
        model=payload.get("model"),
        requested_model=body.get("model"),
        max_tokens=payload.get("max_tokens"),
        thinking=payload.get("thinking"),
        reasoning_effort=payload.get("reasoning_effort"),
        requested_effort=requested_effort,
        tools=len(payload.get("tools") or []),
        messages=len(payload.get("messages") or []),
        system_len=sum(len(_text_of(message.get("content"))) for message in payload.get("messages", [])
                       if message.get("role") == "system"),
        stream=payload.get("stream"),
    )
    if BROWSER_DEBUG_MESSAGES:
        _dbg("payload_messages", messages=_message_shape(payload.get("messages") or []))
    _maybe_break_tool_loop(payload)
    return payload


def build_headers() -> dict[str, str]:
    if UPSTREAM_KIND == "browser":
        return {}
    if UPSTREAM_KIND == "nvcf":
        if not NVCF_URL:
            raise ConfigError("NVCF_URL 未设置")
        cookie = load_cookie()
        token = load_captcha()
        if not cookie:
            raise ConfigError(f"缺少 Cookie：请把抓包里的整行 Cookie 写入 {NV_COOKIE_FILE}")
        if not token:
            raise ConfigError(f"缺少 Nv-Captcha-Token：请写入 {NV_CAPTCHA_FILE}")
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": NV_ORIGIN,
            "Referer": NV_REFERER,
            "User-Agent": NV_UA,
            "Cookie": cookie,
            "Nv-Captcha-Token": token,
        }

    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if UPSTREAM_KEY:
        headers["Authorization"] = f"Bearer {UPSTREAM_KEY}"
    return headers


def _upstream_url() -> str:
    if UPSTREAM_KIND == "browser":
        return f"browser://cdp:{BROWSER_CDP_PORT}"
    return NVCF_URL if UPSTREAM_KIND == "nvcf" else f"{UPSTREAM_BASE}/chat/completions"


_browser = None


def _get_browser():
    global _browser
    if _browser is None:
        from browser_upstream import BrowserPool, BrowserUpstream

        if BROWSER_POOL_SIZE > 1:
            _browser = BrowserPool(port=BROWSER_CDP_PORT, size=BROWSER_POOL_SIZE, trace=_dbg)
        else:
            _browser = BrowserUpstream(port=BROWSER_CDP_PORT, target_hint=BROWSER_TARGET_HINT)
    return _browser


# --------------------------------------------------------------------------- #
# 上游事件流
# --------------------------------------------------------------------------- #

def _unwrap(chunk: Any) -> dict[str, Any] | None:
    if not isinstance(chunk, dict):
        return None
    if "choices" in chunk:
        return chunk
    for key in ("result", "output", "data"):
        inner = chunk.get(key)
        if isinstance(inner, dict) and "choices" in inner:
            return inner
    return None


def _chunk_events(chunk: Any) -> list[dict[str, Any]]:
    if isinstance(chunk, dict) and chunk.get("error"):
        error = chunk["error"]
        event = {"type": "error", "status": 502, "message": json.dumps(error, ensure_ascii=False)}
        # This is an error *inside* an HTTP 200 SSE stream, not an HTTP status.
        if isinstance(error, dict):
            code = error.get("code", error.get("status"))
            if isinstance(code, str) and code in ("500", "502", "503", "504"):
                code = int(code)
            if type(code) is int:
                event["sse_error_code"] = code
        return [event]

    obj = _unwrap(chunk)
    if obj is None:
        return []

    events: list[dict[str, Any]] = []
    usage = obj.get("usage")
    if usage:
        events.append({"type": "usage", "usage": usage})

    for choice in obj.get("choices") or []:
        delta = choice.get("delta") or {}

        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            events.append({"type": "thinking", "text": reasoning})

        text = delta.get("content")
        if text:
            events.append({"type": "text", "text": text})

        for call in delta.get("tool_calls") or []:
            fn = call.get("function") or {}
            events.append(
                {
                    "type": "tool",
                    "index": int(call.get("index", 0)),
                    "id": call.get("id"),
                    "name": fn.get("name"),
                    "args": fn.get("arguments"),
                }
            )

        if choice.get("finish_reason"):
            events.append({"type": "finish", "reason": choice["finish_reason"]})

    return events


async def _events_from_text(text: str) -> AsyncIterator[dict[str, Any]]:
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        if raw == "[DONE]":
            break
        try:
            chunk = json.loads(raw)
        except json.JSONDecodeError:
            continue
        events = _chunk_events(chunk)
        for ev in events:
            yield ev
        if events and events[-1].get("type") == "error":
            return


def _sse_text_and_tools(text: str) -> tuple[str, bool]:
    parts: list[str] = []
    has_tool = False
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            chunk = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                parts.append(delta["content"])
            if delta.get("tool_calls"):
                has_tool = True
    return "".join(parts).strip(), has_tool


def _sse_summary(text: str) -> tuple[str, bool, str | None, int]:
    parts: list[str] = []
    has_tool = False
    finish: str | None = None
    thinking_chars = 0
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            chunk = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                parts.append(delta["content"])
            thinking = delta.get("reasoning_content") or delta.get("reasoning")
            if thinking:
                thinking_chars += len(thinking)
            if delta.get("tool_calls"):
                has_tool = True
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    return "".join(parts).strip(), has_tool, finish, thinking_chars


def _is_upstream_pool_timeout(event: dict[str, Any]) -> bool:
    message = str(event.get("message", ""))
    return (event.get("status") == 500 and "Pool#acquire(Duration)" in message
            and "has been pending for more than the configured timeout" in message)


async def _browser_retry_pause(delay: float, deadline: float) -> AsyncIterator[dict[str, Any]]:
    """Release the failed stream before waiting; cancellation interrupts sleep."""
    until = min(deadline, time.monotonic() + delay)
    if BROWSER_HEARTBEAT_SEC > 0:
        yield {"type": "ping"}
    while (remaining := until - time.monotonic()) > 0:
        await asyncio.sleep(min(remaining, BROWSER_HEARTBEAT_SEC) if BROWSER_HEARTBEAT_SEC > 0 else remaining)
        if BROWSER_HEARTBEAT_SEC > 0:
            yield {"type": "ping"}


async def _browser_events_with_retry(payload: dict[str, Any], *, buffered: bool,
                                     defer_output: bool = False) -> AsyncIterator[dict[str, Any]]:
    from browser_events import browser_events

    req_id = _request_id.get() or uuid.uuid4().hex[:12]
    started = time.monotonic()
    deadline = started + BROWSER_TIMEOUT
    current = payload
    attempt = pool_retries = http_retries = fetch_retries = stream_retries = 0
    placeholder_retried = False
    while True:
        attempt += 1
        committed = False
        thinking_delivered = False
        has_text = has_tool = False
        marker = "[Tool use interrupted]"
        pending_text = ""
        buffered_events: list[dict[str, Any]] = []
        finish = None
        usage: dict[str, Any] = {}
        counts = {"thinking_chars": 0, "text_chars": 0, "tool_calls": 0}
        tool_indexes: set[int] = set()
        last_progress = started
        last_delivery = time.monotonic()

        def log(event: str, **data: Any) -> None:
            _dbg(event, id=req_id, request_id=req_id, attempt=attempt,
                 sec=round(time.monotonic() - started, 3), **data)

        if time.monotonic() >= deadline:
            log("browser_error", error="浏览器请求总时限已到")
            yield {"type": "error", "status": 502, "message": "浏览器请求总时限已到"}
            return
        log("browser_start", max_tokens=current.get("max_tokens"),
            reasoning_effort=current.get("reasoning_effort"),
            messages=len(current.get("messages") or []), tools=len(current.get("tools") or []),
            output_deferred=defer_output)
        deferred = None
        validator = AnthropicBlocks() if defer_output else None
        try:
            if defer_output:
                deferred = DeferredEvents()

            def stage(item: dict[str, Any]) -> bool:
                """Return true only for leading thinking that can be shown now."""
                validator.feed(item)
                if item["type"] == "thinking" and not buffered and not deferred.output_started:
                    return True
                deferred.append(item)
                return False

            failure = None
            async with aclosing(browser_events(
                _get_browser(), current, max(0, deadline - time.monotonic()),
                BROWSER_HEARTBEAT_SEC, _chunk_events, log,
            )) as source:
                async for event in source:
                    kind = event["type"]
                    if kind == "error":
                        failure = event
                        break
                    if kind == "ping":
                        yield event
                        last_delivery = time.monotonic()
                        continue
                    if (defer_output and BROWSER_HEARTBEAT_SEC > 0
                            and time.monotonic() - last_delivery >= BROWSER_HEARTBEAT_SEC):
                        yield {"type": "ping"}
                        last_delivery = time.monotonic()
                    if kind == "usage":
                        usage = event["usage"]
                    if kind == "finish":
                        finish = event["reason"]
                    if kind == "thinking":
                        counts["thinking_chars"] += len(event["text"])
                    if kind == "text":
                        counts["text_chars"] += len(event["text"])
                    if kind == "tool":
                        tool_indexes.add(event["index"])
                        counts["tool_calls"] = len(tool_indexes)
                    if time.monotonic() - last_progress >= 5:
                        log("browser_progress", counts=dict(counts), usage=usage)
                        last_progress = time.monotonic()

                    batch = [event]
                    if kind == "text" and not has_text and not has_tool:
                        pending_text += event["text"]
                        candidate = pending_text.strip()
                        if len(pending_text) <= 256 and (not candidate or marker.startswith(candidate)):
                            continue
                        has_text = bool(candidate)
                        batch = [{"type": "text", "text": pending_text}]
                        pending_text = ""
                    elif kind == "tool":
                        has_tool = True
                        if pending_text:
                            batch.insert(0, {"type": "text", "text": pending_text})
                            pending_text = ""
                    for item in batch:
                        if defer_output:
                            if stage(item):
                                thinking_delivered = True
                                yield item
                                last_delivery = time.monotonic()
                            continue
                        if buffered and not has_text and not has_tool:
                            buffered_events.append(item)
                            continue
                        if buffered_events:
                            for saved in buffered_events:
                                yield saved
                            buffered_events.clear()
                        if item["type"] in ("thinking", "text", "tool"):
                            if not committed:
                                log("browser_first_content")
                            committed = True
                        yield item

            if failure is None:
                if pending_text.strip() and pending_text.strip() != marker:
                    has_text = True
                    item = {"type": "text", "text": pending_text}
                    if defer_output:
                        stage(item)
                    else:
                        for saved in buffered_events:
                            yield saved
                        buffered_events.clear()
                        committed = True
                        yield item

                bad = not has_text and not has_tool
                # A streamed partial thinking block at its token limit is a valid truncation.
                valid_truncation = finish == "length" and (
                    counts["thinking_chars"] > 0 if defer_output else not buffered and committed)
                if bad and not valid_truncation:
                    kind = "placeholder" if pending_text.strip() == marker else "empty"
                    log(f"{kind}_detected", counts=counts, finish=finish)
                    if PLACEHOLDER_RETRY and not placeholder_retried and (defer_output or buffered or not committed):
                        placeholder_retried = True
                        current = dict(payload)
                        current["max_tokens"] = min(BROWSER_MAX_TOKENS, max(int(payload.get("max_tokens") or 0), 2048))
                        current["messages"] = list(payload.get("messages") or []) + [{
                            "role": "user", "content": "[系统纠正] 上一轮没有有效正文或工具调用。请继续任务，给出真实答案或调用工具，不要输出 [Tool use interrupted]。",
                        }]
                        log(f"{kind}_retry_start")
                        continue
                    log("browser_error", error="上游没有有效正文或工具调用")
                    yield {"type": "error", "status": 502, "message": "上游没有有效正文或工具调用；已交付内容的流不能静默重试"}
                    return
                for saved in buffered_events:
                    yield saved
                if defer_output:
                    validator.finish()
                    log("browser_output_ready", deferred_bytes=deferred.size,
                        thinking_delivered=thinking_delivered, counts=counts, finish=finish)
                    for saved in deferred:
                        if saved["type"] in ("thinking", "text", "tool"):
                            if not committed:
                                log("browser_first_content")
                            committed = True
                        yield saved
                log("browser_done", counts=counts, finish=finish, usage=usage)
                return
        except (asyncio.CancelledError, GeneratorExit):
            log("browser_cancelled")
            raise
        except Exception as exc:
            failure = {"type": "error", "status": int(getattr(exc, "status", 502)), "message": str(exc)}
            if isinstance(exc, ValueError) and defer_output:
                failure["phase"] = "conversion"
                log("browser_validation_failed", error=str(exc), counts=counts, usage=usage)
        finally:
            if deferred is not None:
                deferred.close()

        pool_timeout = _is_upstream_pool_timeout(failure)
        http_failure = failure.get("http_status") in (500, 502, 503, 504)
        fetch_failure = failure.get("fetch_failed") is True
        stream_failure = (failure.get("sse_error_code") in (500, 502, 503, 504)
                          or failure.get("stream_failure") in (
                              "read", "first_byte_timeout", "idle_timeout", "missing_finish"))
        if pool_timeout or http_failure or fetch_failure or stream_failure:
            if pool_timeout:
                retries, limit = pool_retries, BROWSER_POOL_TIMEOUT_RETRIES
                initial_backoff, policy = BROWSER_POOL_TIMEOUT_BACKOFF, "nvidia_pool_acquire_timeout"
            elif http_failure:
                retries, limit = http_retries, BROWSER_HTTP_5XX_RETRIES
                initial_backoff, policy = BROWSER_HTTP_5XX_BACKOFF, "upstream_http_5xx"
            elif fetch_failure:
                retries, limit = fetch_retries, BROWSER_FETCH_RETRIES
                initial_backoff, policy = BROWSER_FETCH_BACKOFF, "browser_fetch_failed"
            else:
                retries, limit = stream_retries, BROWSER_STREAM_RETRIES
                initial_backoff, policy = BROWSER_STREAM_BACKOFF, "upstream_stream_failure"
            remaining = max(0, deadline - time.monotonic())
            backoff = min(30.0, initial_backoff * 2 ** min(retries, 6))
            delay = backoff + random.uniform(0, min(1.0, backoff * 0.2))
            reason = ("content_committed" if committed else
                      "retry_limit" if retries >= limit else
                      "deadline" if remaining <= delay else None)
            if reason is None:
                if pool_timeout:
                    pool_retries += 1
                elif http_failure:
                    http_retries += 1
                elif fetch_failure:
                    fetch_retries += 1
                else:
                    stream_retries += 1
                log("upstream_retry", reason=policy, retry=retries + 1,
                    next_attempt=attempt + 1, delay=round(delay, 3), remaining=round(remaining, 3),
                    status=failure["status"], error=failure["message"], counts=counts, usage=usage,
                    sse_error_code=failure.get("sse_error_code"),
                    stream_failure=failure.get("stream_failure"),
                    output_deferred=defer_output, thinking_delivered=thinking_delivered)
                try:
                    async with aclosing(_browser_retry_pause(delay, deadline)) as pause:
                        async for event in pause:
                            yield event
                except (asyncio.CancelledError, GeneratorExit):
                    log("browser_cancelled")
                    raise
                continue
            log("upstream_retry_skipped", reason=reason, policy=policy, retries=retries, committed=committed,
                remaining=round(remaining, 3))
        log("browser_error", error=failure["message"], status=failure["status"],
            committed=committed, counts=counts, usage=usage,
            output_deferred=defer_output, thinking_delivered=thinking_delivered,
            sse_error_code=failure.get("sse_error_code"), stream_failure=failure.get("stream_failure"))
        yield failure
        return


async def iter_events(payload: dict[str, Any], headers: dict[str, str], *,
                      buffered: bool = True, defer_output: bool = False) -> AsyncIterator[dict[str, Any]]:
    """Give each tool invocation a fresh downstream ID in every API format.

    Some providers reuse IDs such as Read:0 in successive responses. Claude
    Code deduplicates those IDs before sending history back, so input-side
    remapping alone is too late to prevent lost calls and results.
    """
    if HISTORY_REASONING == "omit":
        messages = payload.get("messages") or []
        if any("reasoning_content" in message for message in messages):
            removed = sum(len(message.get("reasoning_content") or "") for message in messages)
            # Do not alter the client's history or stored Responses items. Keep
            # visible text, tool calls/results and all other request controls.
            payload = {**payload, "messages": [
                {key: value for key, value in message.items() if key != "reasoning_content"}
                for message in messages
            ]}
            _dbg("history_reasoning_omitted", removed_chars=removed, messages=len(messages))
    tool_ids: dict[int, str] = {}
    async with aclosing(_iter_upstream_events(payload, headers, buffered=buffered,
                                             defer_output=defer_output)) as source:
        async for event in source:
            if event.get("type") == "tool" and event.get("id"):
                event = dict(event)
                event["id"] = tool_ids.setdefault(event["index"], f"toolu_{uuid.uuid4().hex[:24]}")
            yield event


async def _iter_upstream_events(payload: dict[str, Any], headers: dict[str, str], *,
                               buffered: bool = True, defer_output: bool = False) -> AsyncIterator[dict[str, Any]]:
    """Normalize events; staged Messages output can retry before delivery."""
    if UPSTREAM_KIND == "browser":
        async with aclosing(_browser_events_with_retry(payload, buffered=buffered,
                                                       defer_output=defer_output)) as source:
            async for event in source:
                yield event
        return

    timeout = httpx.Timeout(None, connect=20.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", _upstream_url(), json=payload, headers=headers) as response:
            if UPSTREAM_KIND == "nvcf":
                _refresh_cookie(response)

            if response.status_code >= 400:
                raw = (await response.aread()).decode("utf-8", "ignore")[:1200]
                message = raw
                if response.status_code in (401, 403):
                    message = (
                        f"NVCF 鉴权失败（{response.status_code}）。Cookie 或 Nv-Captcha-Token 已失效，"
                        f"请重新抓包并更新 {NV_COOKIE_FILE} / {NV_CAPTCHA_FILE}。原始响应：{raw}"
                    )
                yield {"type": "error", "status": response.status_code, "message": message}
                return

            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                events = _chunk_events(chunk)
                for ev in events:
                    yield ev
                if events and events[-1].get("type") == "error":
                    return


# --------------------------------------------------------------------------- #
# 响应转换：OpenAI -> Anthropic
# --------------------------------------------------------------------------- #

async def stream_anthropic(
    payload: dict[str, Any], headers: dict[str, str], model: str
) -> AsyncIterator[str]:
    blocks = AnthropicBlocks()
    first_block = False
    yield sse("message_start", {
        "type": "message_start", "message": {
            "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    try:
        async with aclosing(iter_events(payload, headers, buffered=False,
                                       defer_output=BROWSER_MESSAGES_DEFER_OUTPUT)) as source:
            async for event in source:
                if event["type"] == "error":
                    _dbg("messages_failed", phase=event.get("phase", "upstream"), stream=True,
                         error=event["message"][:1500], finish=blocks.reason)
                    yield sse("error", error_body(event["message"]))
                    return
                for kind, data in blocks.feed(event):
                    if kind == "content_block_start" and not first_block:
                        first_block = True
                        _dbg("messages_first_block", kind=data["content_block"]["type"])
                    yield sse(kind, data)
        for kind, data in blocks.finish():
            yield sse(kind, data)
    except (ValueError, httpx.HTTPError) as exc:
        _dbg("messages_failed", phase="conversion", stream=True,
             error=str(exc)[:1500], finish=blocks.reason, tools=len(blocks.tools))
        yield sse("error", error_body(f"upstream stream error: {exc}"))
        return
    if BROWSER_DEBUG_LOG:
        for slot in blocks.tools.values():
            _log_tool_call(slot["id"], slot["name"], json.loads(slot["args"] or "{}"))
    stop_reason = _stop_reason(blocks.reason, bool(blocks.tools))
    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"input_tokens": blocks.usage.get("prompt_tokens", 0),
                  "output_tokens": blocks.usage.get("completion_tokens", 0)},
    })
    yield sse("message_stop", {"type": "message_stop"})
    _dbg("messages_done", stream=True, stop_reason=stop_reason,
         blocks=blocks.index + 1, tools=len(blocks.tools), usage=blocks.usage)


async def aggregate(payload: dict[str, Any], headers: dict[str, str], model: str) -> dict[str, Any]:
    thinking_parts: list[str] = []
    text_parts: list[str] = []
    tools: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    finish: str | None = None

    async with aclosing(iter_events(payload, headers, defer_output=BROWSER_MESSAGES_DEFER_OUTPUT)) as source:
        async for ev in source:
            kind = ev["type"]
            if kind == "error":
                raise UpstreamError(int(ev.get("status") or 502), ev["message"])
            if kind == "usage":
                usage = ev["usage"]
            elif kind == "finish":
                finish = ev["reason"]
            elif kind == "thinking":
                thinking_parts.append(ev["text"])
            elif kind == "text":
                text_parts.append(ev["text"])
            elif kind == "tool":
                slot = tools.setdefault(ev["index"], {"id": None, "name": None, "args": ""})
                if ev["id"]:
                    slot["id"] = ev["id"]
                if ev["name"]:
                    slot["name"] = ev["name"]
                if ev["args"]:
                    slot["args"] += ev["args"]

    if not finish:
        raise UpstreamError(502, "upstream ended without finish_reason")
    content: list[dict[str, Any]] = []
    if thinking_parts:
        content.append({"type": "thinking", "thinking": "".join(thinking_parts), "signature": ""})
    if text_parts:
        content.append({"type": "text", "text": "".join(text_parts)})
    for _, slot in sorted(tools.items()):
        if not slot["id"] or not slot["name"]:
            raise UpstreamError(502, "upstream tool is missing id/name")
        arguments = _loads(slot["args"])
        _log_tool_call(slot["id"], slot["name"], arguments)
        content.append(
            {
                "type": "tool_use",
                "id": slot["id"],
                "name": slot["name"],
                "input": arguments,
            }
        )
    if not content:
        content.append({"type": "text", "text": ""})

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _stop_reason(finish, bool(tools)),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #

@app.get("/health")
async def health() -> dict[str, Any]:
    result = {
        "status": "ok",
        "instance_id": _INSTANCE_ID,
        "pid": os.getpid(),
        "uptime_seconds": round(time.monotonic() - _PROCESS_STARTED, 3),
        "upstream_kind": UPSTREAM_KIND,
        "upstream_url": _upstream_url() if UPSTREAM_KIND == "nvcf" or UPSTREAM_BASE else "",
        "model": UPSTREAM_MODEL,
        "cookie_present": bool(load_cookie()),
        "captcha_present": bool(load_captcha()),
        "tool_ids": "unique_per_invocation",
        "loop_reminder_enabled": bool(LOOP_BREAKER),
        "history_reasoning": HISTORY_REASONING,
    }
    if UPSTREAM_KIND == "browser":
        from browser_upstream import FIRST_BYTE_TIMEOUT, IDLE_TIMEOUT
        result["browser_output_tokens"] = {
            "maximum": BROWSER_MAX_TOKENS, "fixed": BROWSER_FIXED_MAX_TOKENS or None}
        result["browser_timeouts"] = {
            "total_seconds": BROWSER_TIMEOUT, "first_byte_seconds": FIRST_BYTE_TIMEOUT,
            "idle_seconds": IDLE_TIMEOUT}
        result["browser_messages_delivery"] = {
            "thinking": "live", "defer_output": BROWSER_MESSAGES_DEFER_OUTPUT,
            "stream_retries": BROWSER_STREAM_RETRIES}
        result["browser_retry_policy"] = {
            "http_5xx_retries": BROWSER_HTTP_5XX_RETRIES,
            "http_5xx_initial_backoff_seconds": BROWSER_HTTP_5XX_BACKOFF,
            "stream_retries": BROWSER_STREAM_RETRIES,
            "stream_initial_backoff_seconds": BROWSER_STREAM_BACKOFF,
            "backoff_cap_seconds": 30, "jitter_max_seconds": 1,
            "failed_pool_tabs": "refresh_before_reuse"}
        if _browser is not None and hasattr(_browser, "status"):
            result["browser_pool"] = _browser.status()
    return result


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    ids = sorted(ALLOWED_MODELS)
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": 1767225600,
                "owned_by": "nvplay-bridge",
                "type": "model",
                "display_name": model_id,
                "created_at": "2026-01-01T00:00:00Z",
            }
            for model_id in ids
        ],
        "has_more": False,
        "first_id": ids[0],
        "last_id": ids[-1],
    }


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    body = await request.json()
    try:
        validate_model(body.get("model"))
    except ModelNotSupported as exc:
        available = ", ".join(sorted(ALLOWED_MODELS))
        return JSONResponse(
            status_code=404,
            content=error_body(f"model '{exc}' is not supported; available: {available}", "not_found_error"),
        )
    raw = json.dumps(body, ensure_ascii=False)
    return {"input_tokens": max(1, len(raw) // 3)}


@app.post("/v1/messages")
async def create_message(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content=error_body("invalid JSON body", "invalid_request_error"))

    override = request.headers.get("x-reasoning-effort")
    if override:
        body["_effort_override"] = override

    try:
        model = validate_model(body.get("model"))
    except ModelNotSupported as exc:
        available = ", ".join(sorted(ALLOWED_MODELS))
        _dbg("model_rejected", model=str(exc), allowed=sorted(ALLOWED_MODELS))
        return JSONResponse(
            status_code=404,
            content=error_body(f"model '{exc}' is not supported; available: {available}", "not_found_error"),
        )
    body["model"] = model

    try:
        _log_recent_tool_results(body)
        payload = build_payload(body)
        headers = build_headers()
    except ConfigError as exc:
        return JSONResponse(status_code=503, content=error_body(str(exc)))
    except ValueError as exc:
        return JSONResponse(status_code=400, content=error_body(str(exc), "invalid_request_error"))

    model = body.get("model") or UPSTREAM_MODEL

    if body.get("stream"):
        return DisconnectStreamingResponse(
            stream_anthropic(payload, headers, model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    generation = asyncio.create_task(aggregate(payload, headers, model))
    disconnected = asyncio.create_task(_disconnected(request))
    try:
        completed, _ = await asyncio.wait((generation, disconnected), return_when=asyncio.FIRST_COMPLETED)
        if generation not in completed:
            return JSONResponse(status_code=499, content=error_body("client disconnected"))
        message = await generation
    except UpstreamError as exc:
        _dbg("messages_failed", phase="upstream_or_conversion", stream=False, error=exc.message[:1500])
        status = 502 if exc.status >= 500 else exc.status
        return JSONResponse(status_code=status, content=error_body(exc.message))
    except httpx.HTTPError as exc:
        _dbg("messages_failed", phase="upstream", stream=False, error=str(exc)[:1500])
        return JSONResponse(status_code=502, content=error_body(f"upstream error: {exc}"))
    finally:
        generation.cancel()
        disconnected.cancel()
        await asyncio.gather(generation, disconnected, return_exceptions=True)

    _dbg("messages_done", stream=False, stop_reason=message["stop_reason"],
         blocks=len(message["content"]), usage=message["usage"])
    return JSONResponse(content=message)


# Defer binding until the shared backend functions have been defined.
from responses_api import create_router as _responses_router

app.include_router(_responses_router(sys.modules[__name__]))
