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
    - 工具参数在 Anthropic 流里整段下发，不是逐字符 input_json_delta
    - browser 模式整段缓冲，没有真正的增量输出，只靠 ping 保活
    - 没有限流、断线重连；NVCF 会话是单账号单进程
"""

from __future__ import annotations

import asyncio
import json
import os
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
# Kimi K3.1 Pro 官方最大输出 128K tokens。
BROWSER_MAX_TOKENS = int(os.getenv("BROWSER_MAX_TOKENS", "131072"))
BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT", "600"))
BROWSER_HEARTBEAT_SEC = float(os.getenv("BROWSER_HEARTBEAT_SEC", "10"))
BROWSER_POOL_SIZE = int(os.getenv("BROWSER_POOL_SIZE", "3"))
BROWSER_DEBUG_LOG = os.getenv("BROWSER_DEBUG_LOG", "")
PLACEHOLDER_RETRY = int(os.getenv("PLACEHOLDER_RETRY", "1"))
LOOP_BREAKER = int(os.getenv("LOOP_BREAKER", "1"))
BROWSER_DEBUG_MESSAGES = os.getenv("BROWSER_DEBUG_MESSAGES", "") == "1"


def _dbg(event: str, **data: Any) -> None:
    if not BROWSER_DEBUG_LOG:
        return
    try:
        record = {"ts": time.time(), "event": event, **data}
        with open(BROWSER_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass

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

app = FastAPI(
    title="nvplay-bridge",
    version="0.3.0",
    description="Anthropic-compatible bridge for Kimi K3.1 Pro via NVIDIA Build Playground",
)


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
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


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
    # CC 的 tool_use id 可能是 "Bash:0" 这类按轮次复用的短 id；
    # OpenAI 侧需要全局唯一，否则历史里多次调用同名 id 会让模型误以为没执行。
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


_loop_state: dict[str, tuple[tuple[Any, Any], int]] = {}
_loop_state_lock = threading.Lock()


def _conversation_key(messages: list[dict[str, Any]]) -> str:
    for message in messages:
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str) and content.strip():
            return content[:200]
    return ""


def _maybe_break_tool_loop(payload: dict[str, Any]) -> None:
    """连续重复同一工具调用时注入“不要重复、直接总结”的纠正。

    CC 可能压缩历史，单次 payload 里只剩最近一次调用，因此同时维护
    跨请求的会话计数。
    """
    if not LOOP_BREAKER:
        return
    messages = payload.get("messages") or []
    if any("[系统纠正] 同一个工具调用" in str(m.get("content") or "") for m in messages):
        return
    calls: list[tuple[Any, Any]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            calls.append(_tool_loop_key(call))
    if not calls:
        return

    repeated_in_payload = len(calls) >= 3 and all(call == calls[-1] for call in calls[-3:])
    key = _conversation_key(messages)
    with _loop_state_lock:
        previous = _loop_state.get(key)
        count = previous[1] + 1 if previous and previous[0] == calls[-1] else 1
        _loop_state[key] = (calls[-1], count)

    if not repeated_in_payload and count < 3:
        return

    messages.append(
        {
            "role": "user",
            "content": (
                "[系统纠正] 同一个工具调用已经连续成功执行多次，不要再次调用它；"
                "请直接基于已有结果给出最终总结。"
            ),
        }
    )
    if repeated_in_payload:
        _dbg("loop_breaker", tool=calls[-1][0], count=sum(1 for call in calls if call == calls[-1]))
    else:
        _dbg("loop_breaker_stateful", tool=calls[-1][0], count=count)


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
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}
    if UPSTREAM_KIND in {"nvcf", "browser"}:
        payload["stream_options"]["continuous_usage_stats"] = True
        # Kimi 的 temperature / top_p 是固定值，传了可能 400
        payload.pop("temperature", None)
        payload.pop("top_p", None)
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
        system_len=len(json.dumps(payload.get("system") or "", ensure_ascii=False)),
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
            _browser = BrowserPool(port=BROWSER_CDP_PORT, size=BROWSER_POOL_SIZE)
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
        return [{"type": "error", "status": 502, "message": json.dumps(chunk["error"], ensure_ascii=False)}]

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


async def iter_events(payload: dict[str, Any], headers: dict[str, str]) -> AsyncIterator[dict[str, Any]]:
    """把上游 SSE 归一化成事件：thinking / text / tool / finish / usage / error。"""
    if UPSTREAM_KIND == "browser":
        req_id = uuid.uuid4().hex[:8]
        started = time.time()
        _dbg(
            "browser_start",
            id=req_id,
            max_tokens=payload.get("max_tokens"),
            thinking=payload.get("thinking"),
            reasoning_effort=payload.get("reasoning_effort"),
            messages=len(payload.get("messages") or []),
            tools=len(payload.get("tools") or []),
        )
        try:
            if BROWSER_HEARTBEAT_SEC > 0:
                task = asyncio.create_task(
                    asyncio.to_thread(_get_browser().generate, payload, BROWSER_TIMEOUT)
                )
                while True:
                    try:
                        text = await asyncio.wait_for(
                            asyncio.shield(task), timeout=BROWSER_HEARTBEAT_SEC
                        )
                        break
                    except asyncio.TimeoutError:
                        # 浏览器生成期间没有 SSE 字节；发 ping 让 CC 的流空闲计时器复位。
                        yield {"type": "ping"}
            else:
                text = await asyncio.to_thread(_get_browser().generate, payload, BROWSER_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            _dbg("browser_error", id=req_id, sec=round(time.time() - started, 1), error=str(exc)[:400])
            status = int(getattr(exc, "status", 502) or 502)
            yield {"type": "error", "status": status, "message": f"浏览器桥失败：{exc}"}
            return

        # 模型偶尔只输出 "[Tool use interrupted]"，或只输出 thinking 而没有正文/工具调用。
        # 这两种空回合都会污染 CC 上下文；追加纠正消息重生成一次，仍失败则返回错误。
        if PLACEHOLDER_RETRY:
            content, has_tool, finish_reason, thinking_chars = _sse_summary(text)
            placeholder_bad = content == "[Tool use interrupted]" and not has_tool
            empty_bad = not content and not has_tool
            if placeholder_bad or empty_bad:
                kind = "placeholder" if placeholder_bad else "empty"
                _dbg(
                    f"{kind}_detected",
                    id=req_id,
                    sec=round(time.time() - started, 1),
                    thinking_chars=thinking_chars,
                    finish=finish_reason,
                )
                correction = (
                    "[系统纠正] 上一轮只输出了占位文本 [Tool use interrupted]，没有调用工具。"
                    "请立即继续任务：调用合适的工具或给出真实答案，不要输出 [Tool use interrupted]。"
                    if placeholder_bad
                    else "[系统纠正] 上一轮只有思考，没有正文或工具调用。"
                    "请立即继续任务：给出正文，或调用合适的工具。"
                )
                retry_payload = dict(payload)
                # CC 的 /model 校验可能只给 max_tokens=1；重试时给它足够的正文预算。
                retry_payload["max_tokens"] = max(int(payload.get("max_tokens") or 0), 2048)
                retry_payload["messages"] = list(payload.get("messages") or []) + [
                    {"role": "user", "content": correction}
                ]
                try:
                    _dbg(f"{kind}_retry_start", id=req_id)
                    text2 = await asyncio.to_thread(_get_browser().generate, retry_payload, BROWSER_TIMEOUT)
                    content2, has_tool2, _finish2, _thinking2 = _sse_summary(text2)
                    bad2 = (content2 == "[Tool use interrupted]" and not has_tool2) or (
                        not content2 and not has_tool2
                    )
                    if not bad2:
                        text = text2
                        _dbg(f"{kind}_retry_ok", id=req_id, sec=round(time.time() - started, 1))
                    else:
                        _dbg(f"{kind}_retry_failed", id=req_id)
                        yield {
                            "type": "error",
                            "status": 502,
                            "message": "上游只返回 thinking/占位文本，没有正文或工具调用",
                        }
                        return
                except Exception as exc:  # noqa: BLE001
                    _dbg(f"{kind}_retry_error", id=req_id, error=str(exc)[:300])
                    yield {"type": "error", "status": 502, "message": f"上游空响应重试失败：{exc}"}
                    return

        counts = {"thinking_chunks": 0, "thinking_chars": 0, "text_chunks": 0, "text_chars": 0, "tool_calls": 0}
        finish = None
        for line in text.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                chunk = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                thinking = delta.get("reasoning_content") or delta.get("reasoning")
                if thinking:
                    counts["thinking_chunks"] += 1
                    counts["thinking_chars"] += len(thinking)
                content = delta.get("content")
                if content:
                    counts["text_chunks"] += 1
                    counts["text_chars"] += len(content)
                if delta.get("tool_calls"):
                    counts["tool_calls"] += len(delta["tool_calls"])
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        _dbg(
            "browser_done",
            id=req_id,
            sec=round(time.time() - started, 1),
            bytes=len(text),
            counts=counts,
            finish=finish,
        )
        async for ev in _events_from_text(text):
            yield ev
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
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    yield sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    index = 0
    open_block: str | None = None
    tool_buf: dict[int, dict[str, Any]] = {}
    finish: str | None = None
    usage: dict[str, Any] = {}
    sent_any = False

    async for ev in iter_events(payload, headers):
        kind = ev["type"]
        if kind == "ping":
            yield sse("ping", {"type": "ping"})
            continue
        if kind == "error":
            yield sse("error", error_body(ev["message"]))
            return
        if kind == "usage":
            usage = ev["usage"]
            continue
        if kind == "finish":
            finish = ev["reason"]
            continue

        if kind == "thinking":
            if open_block != "thinking":
                if open_block:
                    yield sse("content_block_stop", {"type": "content_block_stop", "index": index})
                    index += 1
                yield sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                    },
                )
                open_block = "thinking"
            sent_any = True
            yield sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "thinking_delta", "thinking": ev["text"]},
                },
            )

        elif kind == "text":
            if open_block != "text":
                if open_block:
                    yield sse("content_block_stop", {"type": "content_block_stop", "index": index})
                    index += 1
                yield sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
                open_block = "text"
            sent_any = True
            yield sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": ev["text"]},
                },
            )

        elif kind == "tool":
            slot = tool_buf.setdefault(ev["index"], {"id": None, "name": None, "args": ""})
            if ev["id"]:
                slot["id"] = ev["id"]
            if ev["name"]:
                slot["name"] = ev["name"]
            if ev["args"]:
                slot["args"] += ev["args"]

    if open_block:
        yield sse("content_block_stop", {"type": "content_block_stop", "index": index})
        index += 1

    if not sent_any and not tool_buf:
        yield sse(
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
        )
        yield sse("content_block_stop", {"type": "content_block_stop", "index": index})
        index += 1

    for _, slot in sorted(tool_buf.items()):
        yield sse(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {
                    "type": "tool_use",
                    "id": slot["id"] or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": slot["name"] or "unknown",
                    "input": {},
                },
            },
        )
        yield sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": slot["args"] or "{}"},
            },
        )
        yield sse("content_block_stop", {"type": "content_block_stop", "index": index})
        index += 1

    yield sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": _stop_reason(finish, bool(tool_buf)), "stop_sequence": None},
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        },
    )
    yield sse("message_stop", {"type": "message_stop"})


async def aggregate(payload: dict[str, Any], headers: dict[str, str], model: str) -> dict[str, Any]:
    thinking_parts: list[str] = []
    text_parts: list[str] = []
    tools: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    finish: str | None = None

    async for ev in iter_events(payload, headers):
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

    content: list[dict[str, Any]] = []
    if thinking_parts:
        content.append({"type": "thinking", "thinking": "".join(thinking_parts), "signature": ""})
    if text_parts:
        content.append({"type": "text", "text": "".join(text_parts)})
    for _, slot in sorted(tools.items()):
        content.append(
            {
                "type": "tool_use",
                "id": slot["id"] or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": slot["name"] or "unknown",
                "input": _loads(slot["args"]),
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
    return {
        "status": "ok",
        "upstream_kind": UPSTREAM_KIND,
        "upstream_url": _upstream_url() if UPSTREAM_KIND == "nvcf" or UPSTREAM_BASE else "",
        "model": UPSTREAM_MODEL,
        "cookie_present": bool(load_cookie()),
        "captcha_present": bool(load_captcha()),
    }


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    ids = sorted(ALLOWED_MODELS)
    return {
        "data": [
            {
                "id": model_id,
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
        payload = build_payload(body)
        headers = build_headers()
    except ConfigError as exc:
        return JSONResponse(status_code=503, content=error_body(str(exc)))

    model = body.get("model") or UPSTREAM_MODEL

    if body.get("stream"):
        return StreamingResponse(
            stream_anthropic(payload, headers, model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    try:
        message = await aggregate(payload, headers, model)
    except UpstreamError as exc:
        status = 502 if exc.status >= 500 else exc.status
        return JSONResponse(status_code=status, content=error_body(exc.message))
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=502, content=error_body(f"upstream error: {exc}"))

    return JSONResponse(content=message)
