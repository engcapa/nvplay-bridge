"""Run one CDP stream in its owning thread and deliver bounded async events."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from collections.abc import AsyncIterator, Callable

from browser_upstream import BrowserCancelled, BrowserError
from stream_support import SSEDecoder

BACKPRESSURE_TIMEOUT = 30.0


async def browser_events(browser, payload: dict, timeout: float, heartbeat: float,
                         normalize: Callable, debug: Callable) -> AsyncIterator[dict]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=64)
    cancel = threading.Event()
    done = asyncio.Event()
    terminal_error: list[dict] = []
    deadline = time.monotonic() + timeout

    def put(event: dict) -> None:
        if cancel.is_set():
            raise BrowserCancelled("客户端已取消请求")
        if len(json.dumps(event, ensure_ascii=False).encode("utf-8")) > 16384:
            raise BrowserError("上游事件元数据超过流式队列单项上限")
        future = asyncio.run_coroutine_threadsafe(queue.put(event), loop)
        stalled = time.monotonic()
        try:
            while True:
                try:
                    future.result(timeout=0.05)
                    return
                except concurrent.futures.TimeoutError:
                    if cancel.is_set():
                        raise BrowserCancelled("客户端已取消请求")
                    if time.monotonic() - stalled >= BACKPRESSURE_TIMEOUT:
                        raise BrowserError("客户端读取过慢，流式队列持续阻塞")
                    if time.monotonic() >= deadline:
                        raise BrowserError("浏览器请求总时限已到")
        finally:
            if not future.done():
                future.cancel()

    def publish(event: dict) -> None:
        # Bound each queue item as well as queue length (<= 1 MiB of text).
        field = "text" if event["type"] in ("text", "thinking") else "args"
        value = event.get(field)
        if isinstance(value, str) and len(value) > 2048:
            for start in range(0, len(value), 2048):
                part = dict(event)
                part[field] = value[start:start + 2048]
                if start and event["type"] == "tool":
                    part["id"] = part["name"] = None
                put(part)
        else:
            put(event)

    def worker() -> None:
        source = None
        parser = SSEDecoder()
        finished = False
        first = True
        try:
            source = browser.stream(payload, timeout, cancel, trace=debug)
            for text in source:
                if first:
                    debug("browser_first_data")
                    first = False
                for chunk in parser.feed(text):
                    for event in normalize(chunk):
                        finished = finished or event["type"] == "finish"
                        publish(event)
                        if event["type"] == "error":
                            return
            for chunk in parser.feed("", final=True):
                for event in normalize(chunk):
                    finished = finished or event["type"] == "finish"
                    publish(event)
                    if event["type"] == "error":
                        return
            if not finished:
                raise BrowserError("上游响应缺少 finish_reason，可能已截断")
        except BrowserCancelled:
            pass
        except Exception as exc:
            if not cancel.is_set():
                # Control errors must not block again on an already full data queue.
                terminal_error.append({"type": "error", "status": int(getattr(exc, "status", 502)),
                                       "message": str(exc)[:2000]})
        finally:
            try:
                if source is not None:
                    source.close()
            finally:
                try:
                    loop.call_soon_threadsafe(done.set)
                except RuntimeError:
                    pass

    thread = threading.Thread(target=worker, name="browser-stream", daemon=True)
    thread.start()
    last_sent = time.monotonic()
    try:
        while not done.is_set() or not queue.empty():
            if heartbeat > 0 and time.monotonic() - last_sent >= heartbeat:
                yield {"type": "ping"}
                last_sent = time.monotonic()
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            yield event
            if event["type"] not in ("usage", "finish"):
                last_sent = time.monotonic()
        if terminal_error:
            yield terminal_error[0]
    finally:
        cancel.set()
        try:
            await asyncio.wait_for(asyncio.shield(done.wait()), timeout=6)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            debug("browser_cleanup_pending")
