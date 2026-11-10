"""browser_upstream.py — 通过 CDP 驱动 Playground 页面完成一次推理请求。

原理：
  1. 页面自己点一次“发送”，由页面 JS 现场签发 nv-captcha-token；
  2. 我们在 fetch 层把请求体替换成目标 body（可含 tools）；
  3. 克隆响应流，通过 CDP 取走新增 SSE；取消时中止页面 fetch。

前提：Brave 以 --remote-debugging-port=9222 启动，并打开 Playground 页面。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlparse
import uuid
from collections.abc import Callable, Iterator

import websocket

DEFAULT_HINT = "kimi-k3_1-pro/playground"
DEFAULT_BASE_URL = "https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground"
QUEUE_TIMEOUT = float(os.getenv("BROWSER_QUEUE_TIMEOUT", "120"))
TAB_MAX_TURNS = int(os.getenv("BROWSER_TAB_MAX_TURNS", "50"))
TAB_MAX_TEXT_LEN = int(os.getenv("BROWSER_TAB_MAX_TEXT_LEN", "500000"))
POLL_INTERVAL = float(os.getenv("BROWSER_POLL_INTERVAL", "0.1"))
FIRST_BYTE_TIMEOUT = float(os.getenv("BROWSER_FIRST_BYTE_TIMEOUT", os.getenv("BROWSER_TIMEOUT", "3600")))
IDLE_TIMEOUT = float(os.getenv("BROWSER_IDLE_TIMEOUT", "90"))
TAB_READY_TIMEOUT = max(0.1, float(os.getenv("BROWSER_TAB_READY_TIMEOUT", "30")))
TAB_RECOVERY_ATTEMPTS = max(1, int(os.getenv("BROWSER_TAB_RECOVERY_ATTEMPTS", "2")))
TAB_RECOVERY_BACKOFF = max(0.1, float(os.getenv("BROWSER_TAB_RECOVERY_BACKOFF", "5")))
TAB_REACTIVATE_INTERVAL = 3.0
PAGE_BUFFER_BYTES = 4 * 1024 * 1024
DRAIN_BYTES = 64 * 1024
ERROR_BODY_TIMEOUT = 5.0
ERROR_BODY_CHARS = 8192
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _matches_hint(url: str, hint: str) -> bool:
    if "=" in hint and "/" not in hint:
        key, _, value = hint.partition("=")
        return parse_qs(urlparse(url).query).get(key) == [value]
    return hint in url

PATCH_JS = r"""
(() => {
  if (window.__bridgeVersion === 3) return 'already';
  if (window.__bridgeInstalled && !window.__bridgeOriginalFetch) return 'reload-required';
  const prev = window.__bridgeOriginalFetch || window.fetch.bind(window);
  window.__bridgeOriginalFetch = prev;
  window.__bridgeVersion = 3;
  window.__bridgeInstalled = true;
  const encoder = new TextEncoder();
  window.__bridgeArm = (id, body, limit) => {
    if (window.__bridge && !window.__bridge.done) throw new Error('previous request still active');
    window.__bridge = { id, body, armed: true, chunks: [], pending: 0, peak: 0,
      limit, status: 0, done: false, error: null, ct: null, controller: null, reader: null };
  };
  window.__bridgeDrain = (id, limit) => {
    const b = window.__bridge;
    if (!b || b.id !== id) throw new Error('bridge request id mismatch');
    let data = '', size = 0;
    while (b.chunks.length && size + b.chunks[0].bytes <= limit) {
      const c = b.chunks.shift(); data += c.text; size += c.bytes; b.pending -= c.bytes;
    }
    return { data, status: b.status, ct: b.ct, done: b.done && !b.chunks.length,
      error: b.chunks.length ? null : b.error, peak: b.peak };
  };
  window.__bridgeAbort = async id => {
    const b = window.__bridge;
    if (!b || b.id !== id) return false;
    b.armed = false; b.body = null;
    if (b.controller) b.controller.abort(); else b.done = true;
    if (b.reader) b.reader.cancel().catch(() => {});
    for (let i = 0; i < 40 && !b.done; i++) await new Promise(r => setTimeout(r, 50));
    b.chunks = []; b.pending = 0;
    return b.done;
  };
  window.fetch = function (input, init) {
    let url = typeof input === 'string' ? input
      : (input instanceof Request ? input.url : String(input));
    const b = window.__bridge;
    if (!url.includes('/v2/predict/models/') || !b || !b.armed) return prev(input, init);
    b.armed = false;
    const body = b.body; b.body = null;
    const controller = new AbortController(); b.controller = controller;
    let req, originalSignal, relay;
    try {
      const original = new Request(input, init);
      originalSignal = original.signal;
      relay = () => controller.abort();
      if (originalSignal.aborted) relay();
      else originalSignal.addEventListener('abort', relay, { once: true });
      const h = new Headers(original.headers);
      h.delete('host'); h.delete('content-length');
      req = new Request(original, { headers: h, body, credentials: 'include',
        signal: controller.signal });
    } catch (e) { b.error = String(e); b.done = true; return Promise.reject(e); }
    const append = text => {
      for (let start = 0; start < text.length;) {
        let end = Math.min(start + 8192, text.length);
        if (end < text.length && /[\uD800-\uDBFF]/.test(text[end - 1])) end--;
        const part = text.slice(start, end), bytes = encoder.encode(part).length;
        if (b.pending + bytes > b.limit) throw new Error('bridge page buffer exceeded');
        b.chunks.push({ text: part, bytes }); b.pending += bytes;
        b.peak = Math.max(b.peak, b.pending); start = end;
      }
    };
    const p = prev(req);
    p.then(async r => {
      b.status = r.status;
      b.ct = r.headers.get('content-type');
      try {
        const reader = r.clone().body.getReader();
        b.reader = reader;
        const dec = new TextDecoder();
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          append(dec.decode(value, { stream: true }));
        }
        append(dec.decode());
      } catch (e) { b.error = String(e); controller.abort(); }
      finally { b.done = true; originalSignal.removeEventListener('abort', relay); }
    }).catch(e => {
      b.status = -1; b.error = String(e); b.done = true;
      if (originalSignal) originalSignal.removeEventListener('abort', relay);
    });
    return p;
  };
  return 'installed';
})()
"""

DISMISS_JS = r"""
(() => {
  const hit = re => {
    const b = [...document.querySelectorAll('button')].find(x => re.test(x.innerText || ''));
    if (b) { b.click(); return b.innerText.trim(); }
    return null;
  };
  return {
    consent: hit(/Reject Optional|Accept All/i),
    ack: hit(/Acknowledge\s*&\s*Continue/i) || hit(/Close Modal/i)
  };
})()
"""

PAGE_STATE_JS = r"""
(() => {
  const ta = document.querySelector('textarea[aria-label="chat prompt"]');
  const send = document.querySelector('button[aria-label="Send"]');
  const b = window.__bridge;
  return {
    ready: document.readyState,
    ta: !!ta, editable: !!ta && !ta.disabled && !ta.readOnly,
    send: !!send, hc: typeof window.hcaptcha,
    captcha_ready: !!window.hcaptcha && typeof window.hcaptcha.execute === 'function',
    visibility: document.visibilityState, focus: document.hasFocus(),
    was_discarded: document.wasDiscarded === true,
    body_chars: document.body ? document.body.innerText.length : 0,
    request_active: !!b && !b.done,
    patch_upgrade_required: !!window.__bridgeInstalled && !window.__bridgeOriginalFetch
  };
})()
"""


def _page_ready(state: dict) -> bool:
    # Text length varies with rendering/visibility and is diagnostic only.
    return bool(state.get("ready") == "complete" and state.get("ta")
                and state.get("editable") and state.get("send") and state.get("captcha_ready"))


WAIT_JS = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  for (let i = 0; i < 80; i++) {
    const ta = document.querySelector('textarea[aria-label="chat prompt"]');
    const send = document.querySelector('button[aria-label="Send"]');
    if (document.readyState === 'complete' && ta && send) {
      await sleep(500);
      return 'ready';
    }
    await sleep(500);
  }
  return 'not-ready';
})()
"""

TRIGGER_JS = r"""
(async () => {
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const ta = document.querySelector('textarea[aria-label="chat prompt"]');
  if (!ta) return 'no textarea';
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  setter.call(ta, 'x');
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  await sleep(800);
  const btn = [...document.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === 'Send');
  if (!btn) return 'no send button';
  if (btn.disabled) return 'send disabled';
  btn.click();
  return 'clicked';
})()
"""


class BrowserError(RuntimeError):
    pass


class BrowserCancelled(BrowserError):
    pass


class BrowserPreparationError(BrowserError):
    """Preparation failed before arming/clicking; another tab may safely be tried."""


def _check(cancel: threading.Event, deadline: float) -> None:
    if cancel.is_set():
        raise BrowserCancelled("客户端已取消请求")
    if time.monotonic() >= deadline:
        raise BrowserError("浏览器请求总时限已到")


def _pause(cancel: threading.Event, deadline: float, seconds: float) -> None:
    _check(cancel, deadline)
    cancel.wait(max(0, min(seconds, deadline - time.monotonic())))
    _check(cancel, deadline)


class BrowserHTTPError(BrowserError):
    """上游返回非 200 时携带真实 HTTP 状态码。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _connect_cdp(url: str, timeout: float):
    try:
        return websocket.create_connection(url, timeout=timeout)
    except Exception as exc:
        exc.cdp_phase = "connect"
        raise


def _read_cdp(ws, method: str):
    try:
        return json.loads(ws.recv())
    except Exception as exc:
        exc.cdp_phase = "receive"
        exc.cdp_method = method
        raise


class BrowserUpstream:
    def __init__(self, port: int = 9222, target_hint: str = DEFAULT_HINT) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.hint = target_hint
        self.ws = None
        self.seq = 0
        self._lock = threading.Lock()
        self._pool_managed = False
        self.needs_refresh = False
        self.peak_buffer_bytes = 0

    # ---------------- CDP 基础 ----------------
    def _target(self) -> dict:
        try:
            with LOCAL_HTTP.open(f"{self.base}/json/list", timeout=5) as resp:
                targets = json.load(resp)
        except OSError as exc:
            raise BrowserError(
                f"CDP 未就绪（{self.base}）。请用 --remote-debugging-port={self.base.rsplit(':', 1)[-1]} 启动 Brave 并打开 Playground。"
            ) from exc
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        for t in pages:
            if _matches_hint(t.get("url") or "", self.hint):
                return t
        raise BrowserError(f"找不到页面 target: {self.hint}，请先在 Brave 打开 Playground")

    def _connect(self) -> None:
        if self.ws is not None:
            return
        target = self._target()
        self.ws = _connect_cdp(target["webSocketDebuggerUrl"], timeout=5)
        self._call("Runtime.enable")

    def _call(self, method: str, params: dict | None = None, timeout: float = 5.0):
        if self.ws is None:
            self._connect()
        self.seq += 1
        mid = self.seq
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.ws.settimeout(max(0.1, deadline - time.monotonic()))
            msg = _read_cdp(self.ws, method)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise BrowserError(str(msg["error"]))
                return msg.get("result", {})
        raise BrowserError(f"CDP timeout: {method}")

    def _eval(self, expr: str, timeout: float = 5.0):
        res = self._call(
            "Runtime.evaluate",
            {"expression": expr, "awaitPromise": True, "returnByValue": True},
            timeout,
        )
        if res.get("exceptionDetails"):
            raise BrowserError(json.dumps(res["exceptionDetails"])[:800])
        return res.get("result", {}).get("value")

    # ---------------- 公开接口 ----------------
    def _prepare_page(self, cancel: threading.Event, deadline: float) -> None:
        """关闭 cookie/免责声明弹窗，并等待页面重载稳定。

        首次使用全新 profile 时，点击 “Acknowledge & Continue” 可能触发整页重载；
        必须在重载完成后再注入 fetch 补丁，否则补丁会随旧上下文一起丢失。
        """
        for _ in range(4):
            self._eval(DISMISS_JS)
            self._wait_ready(cancel, deadline)
            try:
                res = self._eval(DISMISS_JS) or {}
            except BrowserError:
                res = {}
            if res.get("ack") or res.get("consent"):
                _pause(cancel, deadline, 2.5)
                continue
            try:
                has_modal = self._eval(
                    "!![...document.querySelectorAll('button')]"
                    ".find(b=>/Acknowledge\\s*&\\s*Continue/i.test(b.innerText||''))"
                )
            except BrowserError:
                has_modal = True
            if not has_modal:
                return
            _pause(cancel, deadline, 1.5)
        raise BrowserError("免责声明弹窗未关闭")

    def _wait_ready(self, cancel: threading.Event, deadline: float) -> None:
        ready_deadline = min(deadline, time.monotonic() + 120)
        state = {}
        while time.monotonic() < ready_deadline:
            _check(cancel, deadline)
            try:
                state = self._eval(PAGE_STATE_JS) or {}
            except BrowserError as exc:
                state = {"context_error": str(exc)[:300]}
            if _page_ready(state):
                return
            cancel.wait(min(0.2, max(0, ready_deadline - time.monotonic())))
        _check(cancel, float("inf"))
        raise BrowserError("页面未就绪: " + json.dumps(state, ensure_ascii=False))

    def page_text_len(self) -> int:
        try:
            value = self._eval("document.body ? document.body.innerText.length : 0", timeout=10)
            return int(value or 0)
        except Exception:  # noqa: BLE001
            return 0

    def generate(self, body: dict, timeout: float = 3600.0) -> str:
        return "".join(self.stream(body, timeout))

    def stream(self, body: dict, timeout: float = 3600.0,
               cancel: threading.Event | None = None, trace: Callable | None = None) -> Iterator[str]:
        cancel = cancel if cancel is not None else threading.Event()
        deadline = time.monotonic() + timeout
        queued_until = min(deadline, time.monotonic() + QUEUE_TIMEOUT)
        while not self._lock.acquire(timeout=0.05):
            _check(cancel, deadline)
            if time.monotonic() >= queued_until:
                raise BrowserError(f"浏览器桥排队超时（>{QUEUE_TIMEOUT:.0f}s）")
        try:
            _check(cancel, deadline)
            yield from self._stream_locked(body, cancel, deadline, trace)
        finally:
            self._lock.release()

    def _prepare_for_send(self, cancel: threading.Event, deadline: float) -> None:
        self._connect()
        if self.needs_refresh:
            self._call("Page.reload")
            _pause(cancel, deadline, 0.5)
        self._prepare_page(cancel, deadline)
        installed = self._eval(PATCH_JS)
        if installed == "reload-required":
            self._call("Page.reload")
            _pause(cancel, deadline, 0.5)
            self._prepare_page(cancel, deadline)
            installed = self._eval(PATCH_JS)
        if installed not in ("installed", "already"):
            self.needs_refresh = True
            raise BrowserError("页面补丁需要重载后升级")
        self.needs_refresh = False

    def _http_error(self, request_id: str, state: dict, cancel: threading.Event,
                    deadline: float, trace: Callable | None) -> BrowserHTTPError:
        """Wait briefly for the body: fetch publishes headers before reader data."""
        status = int(state["status"])
        read_deadline = min(deadline, time.monotonic() + ERROR_BODY_TIMEOUT)
        body = ""
        reason = "complete"
        while True:
            _check(cancel, deadline)
            data = state.get("data") or ""
            remaining = ERROR_BODY_CHARS - len(body)
            body += data[:remaining]
            self.peak_buffer_bytes = max(self.peak_buffer_bytes, state.get("peak", 0))
            if len(data) > remaining:
                reason = "size_limit"
                break
            if state.get("error"):
                reason = "read_error"
                break
            if state.get("done"):
                break
            if len(body) >= ERROR_BODY_CHARS:
                reason = "size_limit"
                break
            if time.monotonic() >= read_deadline:
                reason = "body_timeout"
                break
            _pause(cancel, deadline, min(POLL_INTERVAL, max(0, read_deadline - time.monotonic())))
            if time.monotonic() >= read_deadline:
                reason = "body_timeout"
                break
            state = self._eval(f"window.__bridgeDrain({request_id},{DRAIN_BYTES})",
                               timeout=max(0.001, min(5, read_deadline - time.monotonic())))
        detail = body or state.get("error") or "empty error response body"
        if reason != "complete":
            detail += f" [error body: {reason}]"
        if trace:
            trace("browser_http_error", status=status, body_chars=len(body), body_state=reason)
        return BrowserHTTPError(status, f"上游 {status}: {detail}")

    def _stream_locked(self, body: dict, cancel: threading.Event,
                       deadline: float, trace: Callable | None) -> Iterator[str]:
        try:
            if self._pool_managed:
                # A previously ready tab may have been discarded or navigated.
                # Quarantine it immediately instead of waiting in this lease.
                self._connect()
                state = self._eval(PAGE_STATE_JS) or {}
                if (not _page_ready(state) or state.get("request_active")
                        or state.get("patch_upgrade_required")):
                    raise BrowserPreparationError("借用的标签页已失效: " + json.dumps(state, ensure_ascii=False))
            self._prepare_for_send(cancel, min(deadline, time.monotonic() + TAB_READY_TIMEOUT))
        except BrowserCancelled:
            raise
        except Exception as exc:
            self.needs_refresh = True
            raise BrowserPreparationError(str(exc)) from exc
        request_id = uuid.uuid4().hex
        if trace:
            trace("browser_tab", tab_id=self.hint, page_request_id=request_id)
        quoted_id = json.dumps(request_id)
        self._eval(f"window.__bridgeArm({quoted_id},"
                   f"{json.dumps(json.dumps(body, ensure_ascii=False))},{PAGE_BUFFER_BYTES})")
        completed = False
        self.peak_buffer_bytes = 0
        try:
            trigger = None
            trigger_deadline = min(deadline, time.monotonic() + 30)
            while time.monotonic() < trigger_deadline:
                _check(cancel, deadline)
                trigger = self._eval(TRIGGER_JS)
                if trigger != "send disabled":
                    break
                _pause(cancel, deadline, 0.2)
            if trigger != "clicked":
                raise BrowserError(f"触发发送失败: {trigger}")

            first_deadline = time.monotonic() + FIRST_BYTE_TIMEOUT
            last_data: float | None = None
            while True:
                _check(cancel, deadline)
                state = self._eval(f"window.__bridgeDrain({quoted_id},{DRAIN_BYTES})")
                self.peak_buffer_bytes = max(self.peak_buffer_bytes, state.get("peak", 0))
                status = int(state.get("status") or 0)
                data = state.get("data") or ""
                if status and status != 200:
                    if status > 0:
                        raise self._http_error(quoted_id, state, cancel, deadline, trace)
                    raise BrowserError(f"上游请求未建立: {data[:500] or state.get('error')}")
                if data:
                    if status != 200:
                        raise BrowserError("上游数据缺少成功响应状态")
                    last_data = time.monotonic()
                    yield data
                # A reader can fail after receiving data but before this poll.
                # Drain every queued byte before surfacing its terminal error;
                # the page defers that error while further chunks remain.
                if state.get("error"):
                    raise BrowserError(f"上游响应读取失败: {state['error']}")
                if state.get("done"):
                    if status != 200:
                        raise BrowserError("上游响应未成功建立")
                    completed = True
                    return
                now = time.monotonic()
                if last_data is None and now >= first_deadline:
                    raise BrowserError("等待上游首个数据超时")
                if last_data is not None and now - last_data >= IDLE_TIMEOUT:
                    raise BrowserError("上游响应空闲超时")
                _pause(cancel, deadline, POLL_INTERVAL)
        finally:
            try:
                if not completed:
                    aborted = self._eval(f"window.__bridgeAbort({quoted_id})", timeout=3)
                    if trace:
                        trace("browser_abort", tab_id=self.hint, confirmed=bool(aborted))
                    if not aborted:
                        raise BrowserError("页面未确认中止请求")
                self._eval(f"(()=>{{const b=window.__bridge;if(b&&b.id==={quoted_id})"
                           "{b.body=null;b.armed=false;b.chunks=[];b.pending=0;}})()")
            except Exception as exc:
                # 池在下一次借出前必须重载；不能复用仍有旧流的页面。
                self.needs_refresh = True
                self.close()
                if trace:
                    trace("browser_cleanup_error", tab_id=self.hint, error=str(exc))
            if trace:
                trace("browser_buffer", tab_id=self.hint, peak_bytes=self.peak_buffer_bytes)

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None


class BrowserPool:
    """Lease only healthy tabs; one maintenance thread recovers idle tabs."""

    def __init__(
        self,
        port: int = 9222,
        size: int = 5,
        base_url: str = DEFAULT_BASE_URL,
        prefix: str = "kpool",
        trace: Callable | None = None,
    ) -> None:
        self.port = port
        self.size = max(1, size)
        self.base_url = base_url
        self.prefix = prefix
        self._ups: list[BrowserUpstream] = []
        self._queue: queue.Queue[BrowserUpstream] = queue.Queue()
        self._turns: dict[str, int] = {}
        self._states: dict[str, str] = {}
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, float] = {}
        self._errors: dict[str, str] = {}
        self._targets: dict[str, str] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._ready = False
        self._lock = threading.Lock()
        self._activation_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._trace = trace

    def _log(self, event: str, **data) -> None:
        if self._trace is not None:
            self._trace(event, **data)

    def status(self) -> dict:
        with self._lock:
            return {"size": self.size, "tabs": dict(self._states), "errors": dict(self._errors)}

    def _list_pages(self) -> list[dict]:
        try:
            with LOCAL_HTTP.open(f"http://127.0.0.1:{self.port}/json/list", timeout=5) as resp:
                return [
                    t
                    for t in json.load(resp)
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                ]
        except OSError as exc:
            raise BrowserError(f"CDP 未就绪（{self.port}）: {exc}") from exc

    def _new_blank(self) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/json/new?url=about:blank", method="PUT"
        )
        with LOCAL_HTTP.open(req, timeout=10) as resp:
            return json.load(resp)

    @staticmethod
    def _call(ws, mid: int, method: str, params: dict | None = None, timeout: float = 5):
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ws.settimeout(max(0.01, deadline - time.monotonic()))
            msg = _read_cdp(ws, method)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise BrowserError(str(msg["error"]))
                return msg.get("result", {})
        raise BrowserError(f"CDP timeout: {method}")

    def _inspect_page(self, ws_url: str) -> dict:
        ws = _connect_cdp(ws_url, timeout=5)
        try:
            result = self._call(ws, 1, "Runtime.evaluate",
                                {"expression": PAGE_STATE_JS, "returnByValue": True})
            return result.get("result", {}).get("value") or {}
        finally:
            ws.close()

    def _navigate(self, ws_url: str, url: str) -> dict:
        ws = _connect_cdp(ws_url, timeout=5)
        try:
            self._call(ws, 1, "Page.enable")
            result = self._call(ws, 2, "Page.navigate", {"url": url})
            if result.get("errorText"):
                raise BrowserError("页面导航失败: " + result["errorText"])
            return result
        finally:
            ws.close()

    def _activate_wait(self, ws_url: str, cancel: threading.Event,
                       deadline: float, trace: Callable | None = None,
                       navigation: dict | None = None) -> None:
        _check(cancel, deadline)
        ws = _connect_cdp(ws_url, timeout=5)
        try:
            _check(cancel, deadline)
            self._call(ws, 1, "Page.enable")
            _check(cancel, deadline)
            self._call(ws, 2, "Runtime.enable")
            # These help background tabs without fabricating visibility/captcha state.
            for mid, method, params in (
                (3, "Page.setWebLifecycleState", {"state": "active"}),
                (4, "Emulation.setFocusEmulationEnabled", {"enabled": True}),
            ):
                _check(cancel, deadline)
                try:
                    self._call(ws, mid, method, params)
                except BrowserError as exc:
                    if trace:
                        trace("pool_activation_unsupported", method=method, error=str(exc)[:300])
            ready_deadline = min(deadline, time.monotonic() + TAB_READY_TIMEOUT)
            next_activation = 0.0
            loader_id = (navigation or {}).get("loaderId")
            navigation_committed = not loader_id
            value: dict = {}
            while time.monotonic() < ready_deadline:
                _check(cancel, deadline)
                activate = time.monotonic() >= next_activation
                if activate:
                    self._call(ws, 5, "Page.bringToFront")
                    next_activation = time.monotonic() + TAB_REACTIVATE_INTERVAL
                    if trace:
                        trace("pool_activation", state=value)
                try:
                    if not navigation_committed:
                        tree = self._call(ws, 8, "Page.getFrameTree")
                        frame = tree.get("frameTree", {}).get("frame", {})
                        if frame.get("loaderId") != loader_id:
                            value = {"navigation_pending": True, "loader_id": frame.get("loaderId"),
                                     "expected_loader_id": loader_id}
                            cancel.wait(min(0.2, max(0, ready_deadline - time.monotonic())))
                            continue
                        navigation_committed = True
                    # Consent can block captcha initialization and may itself
                    # navigate again. Only act after the requested reload commits.
                    if activate:
                        dismissed = self._call(ws, 7, "Runtime.evaluate",
                                               {"expression": DISMISS_JS, "returnByValue": True})
                        action = dismissed.get("result", {}).get("value") or {}
                        if action.get("ack") or action.get("consent"):
                            cancel.wait(min(2.5, max(0, ready_deadline - time.monotonic())))
                            continue
                    res = self._call(ws, 6, "Runtime.evaluate",
                                     {"expression": PAGE_STATE_JS, "returnByValue": True})
                    value = res.get("result", {}).get("value") or {}
                    if not value:
                        value = {"context_error": str(res.get("exceptionDetails", "no context"))[:300]}
                except BrowserError as exc:
                    value = {"context_error": str(exc)[:300]}
                if _page_ready(value) and not value.get("request_active"):
                    if trace:
                        trace("pool_page_ready", state=value)
                    return
                cancel.wait(min(0.2, max(0, ready_deadline - time.monotonic())))
            _check(cancel, float("inf"))
            raise BrowserError("池标签页未就绪: " + json.dumps(value, ensure_ascii=False))
        finally:
            ws.close()

    def _ensure(self, cancel: threading.Event, deadline: float) -> None:
        if self._ready:
            return
        while not self._lock.acquire(timeout=0.05):
            _check(cancel, deadline)
        try:
            _check(cancel, deadline)
            if self._stop.is_set():
                raise BrowserError("浏览器池已关闭")
            if self._ready:
                return
            for n in range(1, self.size + 1):
                hint = f"{self.prefix}={n}"
                upstream = BrowserUpstream(port=self.port, target_hint=hint)
                upstream._pool_managed = True
                upstream.needs_refresh = True
                self._ups.append(upstream)
                self._states[hint] = "pending"
            self._ready = True
            self._worker = threading.Thread(target=self._maintain, name="browser-pool-recovery", daemon=True)
            self._worker.start()
        finally:
            self._lock.release()

    def _publish_ready(self, upstream: BrowserUpstream) -> None:
        with self._lock:
            if self._stop.is_set():
                upstream.close()
                self._states[upstream.hint] = "closed"
                return
            upstream.needs_refresh = False
            self._states[upstream.hint] = "ready"
            self._failures.pop(upstream.hint, None)
            self._retry_at.pop(upstream.hint, None)
            self._errors.pop(upstream.hint, None)
            self._queue.put(upstream)
        self._log("pool_ready", tab_id=upstream.hint)

    def _maintain(self) -> None:
        # Inspect all existing tabs before spending a recovery deadline on any one.
        try:
            pages = self._list_pages()
        except Exception as exc:
            pages = []
            self._log("pool_discovery_error", error=str(exc)[:500])
        for upstream in self._ups:
            if self._stop.is_set():
                return
            page = next((p for p in pages if _matches_hint(p.get("url") or "", upstream.hint)), None)
            if page is not None:
                try:
                    state = self._inspect_page(page["webSocketDebuggerUrl"])
                    self._log("pool_probe", tab_id=upstream.hint, state=state)
                    if (_page_ready(state) and not state.get("request_active")
                            and not state.get("patch_upgrade_required")):
                        self._publish_ready(upstream)
                except Exception as exc:
                    self._log("pool_probe_error", tab_id=upstream.hint, error=str(exc)[:500])
        while not self._stop.is_set():
            self._wake.clear()
            upstream = None
            with self._lock:
                candidates = [candidate for candidate in self._ups
                              if self._states[candidate.hint] == "pending" or (
                                  self._states[candidate.hint] == "failed" and
                                  time.monotonic() >= self._retry_at[candidate.hint])]
                if candidates:
                    upstream = min(candidates, key=lambda candidate: (
                        self._states[candidate.hint] != "pending",
                        self._retry_at.get(candidate.hint, 0)))
                    self._states[upstream.hint] = "recovering"
            if upstream is None:
                self._wake.wait(0.2)
                continue
            hint = upstream.hint
            try:
                for attempt in range(1, TAB_RECOVERY_ATTEMPTS + 1):
                    _check(self._stop, float("inf"))
                    self._log("pool_recovery_start", tab_id=hint, recovery_attempt=attempt)
                    try:
                        self._refresh_tab(upstream, self._stop, time.monotonic() + TAB_READY_TIMEOUT)
                        break
                    except BrowserCancelled:
                        raise
                    except Exception as exc:
                        self._log("pool_recovery_error", tab_id=hint, recovery_attempt=attempt,
                                  error=str(exc)[:1500], error_type=type(exc).__name__,
                                  cdp_phase=getattr(exc, "cdp_phase", None),
                                  cdp_method=getattr(exc, "cdp_method", None))
                        if attempt == TAB_RECOVERY_ATTEMPTS:
                            raise
                self._turns[hint] = 0
                self._publish_ready(upstream)
            except BrowserCancelled:
                upstream.close()
                with self._lock:
                    self._states[hint] = "closed"
                return
            except Exception as exc:
                upstream.close()
                with self._lock:
                    failures = self._failures.get(hint, 0) + 1
                    self._failures[hint] = failures
                    self._retry_at[hint] = time.monotonic() + min(60, TAB_RECOVERY_BACKOFF * 2 ** min(failures - 1, 4))
                    self._errors[hint] = str(exc)[:1500]
                    self._states[hint] = "failed"

    def _refresh_tab(self, upstream: BrowserUpstream, cancel: threading.Event,
                     deadline: float) -> None:
        # Activation and navigation are serialized, never held during generation.
        with self._activation_lock:
            _check(cancel, deadline)
            pages = self._list_pages()
            _check(cancel, deadline)
            page = next((p for p in pages if _matches_hint(p.get("url") or "", upstream.hint)), None)
            if page is None:
                page = next((p for p in pages
                             if p.get("webSocketDebuggerUrl") == self._targets.get(upstream.hint)), None)
            if page is None:
                page = self._new_blank()
            self._targets[upstream.hint] = page["webSocketDebuggerUrl"]
            _check(cancel, deadline)
            upstream.close()
            navigation = self._navigate(page["webSocketDebuggerUrl"], f"{self.base_url}?{upstream.hint}")

            def trace(event, **data):
                self._log(event, tab_id=upstream.hint, **data)

            self._activate_wait(page["webSocketDebuggerUrl"], cancel, deadline, trace, navigation)
            _check(cancel, deadline)
            upstream.needs_refresh = False
            upstream._prepare_for_send(cancel, deadline)

    def generate(self, body: dict, timeout: float = 3600.0) -> str:
        return "".join(self.stream(body, timeout))

    def stream(self, body: dict, timeout: float = 3600.0,
               cancel: threading.Event | None = None, trace: Callable | None = None) -> Iterator[str]:
        cancel = cancel if cancel is not None else threading.Event()
        deadline = time.monotonic() + timeout
        self._ensure(cancel, deadline)
        queued_until = min(deadline, time.monotonic() + QUEUE_TIMEOUT)
        while True:
            _check(cancel, deadline)
            if self._stop.is_set():
                raise BrowserError("浏览器池已关闭")
            try:
                upstream = self._queue.get(timeout=0.05)
            except queue.Empty:
                if time.monotonic() >= queued_until:
                    raise BrowserError(f"浏览器池排队超时（>{QUEUE_TIMEOUT:.0f}s），无可用标签页: "
                                       + json.dumps(self.status(), ensure_ascii=False))
                continue
            hint = upstream.hint
            with self._lock:
                if self._stop.is_set():
                    self._states[hint] = "closed"
                    upstream.close()
                    raise BrowserError("浏览器池已关闭")
                try:
                    _check(cancel, deadline)
                except BrowserError:
                    self._queue.put(upstream)
                    raise
                self._states[hint] = "busy"
                self._cancels[hint] = cancel
            retry = False
            try:
                # Stale/dirty entries must never delay a healthy queued tab.
                if upstream.needs_refresh:
                    retry = True
                else:
                    yield from upstream.stream(body, max(0, deadline - time.monotonic()), cancel, trace)
            except BrowserPreparationError as exc:
                retry = True
                upstream.needs_refresh = True
                if trace:
                    trace("browser_prepare_retry", tab_id=hint, error=str(exc)[:1500])
            except BaseException:
                upstream.needs_refresh = True
                raise
            finally:
                # Complete every CDP operation before changing ownership.
                try:
                    turns = self._turns.get(hint, 0) + 1
                    self._turns[hint] = turns
                    if not cancel.is_set() and not upstream.needs_refresh:
                        text_len = upstream.page_text_len()
                        if turns >= TAB_MAX_TURNS or text_len >= TAB_MAX_TEXT_LEN:
                            upstream.needs_refresh = True
                except Exception:
                    upstream.needs_refresh = True
                if trace:
                    trace("browser_pool_release", tab_id=hint, needs_refresh=upstream.needs_refresh)
                with self._lock:
                    self._cancels.pop(hint, None)
                    if self._stop.is_set():
                        self._states[hint] = "closed"
                        upstream.close()
                    elif upstream.needs_refresh or cancel.is_set():
                        upstream.needs_refresh = True
                        self._states[hint] = "pending"
                        self._wake.set()
                    else:
                        self._states[hint] = "ready"
                        self._queue.put(upstream)
            if not retry:
                return
            if time.monotonic() >= queued_until:
                raise BrowserError("浏览器页面准备失败，等待健康标签页的时限已到")

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            for cancel in self._cancels.values():
                cancel.set()
        if self._worker is not None:
            self._worker.join(timeout=10)
        with self._lock:
            for upstream in self._ups:
                if self._states.get(upstream.hint) not in ("busy", "recovering"):
                    upstream.close()
                    self._states[upstream.hint] = "closed"
