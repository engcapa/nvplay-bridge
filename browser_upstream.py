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
FIRST_BYTE_TIMEOUT = float(os.getenv("BROWSER_FIRST_BYTE_TIMEOUT", "120"))
IDLE_TIMEOUT = float(os.getenv("BROWSER_IDLE_TIMEOUT", "90"))
PAGE_BUFFER_BYTES = 4 * 1024 * 1024
DRAIN_BYTES = 64 * 1024
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _matches_hint(url: str, hint: str) -> bool:
    if "=" in hint and "/" not in hint:
        key, _, value = hint.partition("=")
        return parse_qs(urlparse(url).query).get(key) == [value]
    return hint in url

PATCH_JS = r"""
(() => {
  if (window.__bridgeVersion === 2) return 'already';
  if (window.__bridgeInstalled && !window.__bridgeOriginalFetch) return 'reload-required';
  const prev = window.__bridgeOriginalFetch || window.fetch.bind(window);
  window.__bridgeOriginalFetch = prev;
  window.__bridgeVersion = 2;
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
      error: b.error, peak: b.peak };
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


class BrowserUpstream:
    def __init__(self, port: int = 9222, target_hint: str = DEFAULT_HINT) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.hint = target_hint
        self.ws = None
        self.seq = 0
        self._lock = threading.Lock()
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
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=5)
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
            msg = json.loads(self.ws.recv())
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
        while time.monotonic() < ready_deadline:
            _check(cancel, deadline)
            if self._eval("document.readyState==='complete' && !!document.querySelector("
                          "'textarea[aria-label=\"chat prompt\"]') && !!document.querySelector("
                          "'button[aria-label=\"Send\"]')"):
                return
            _pause(cancel, deadline, 0.2)
        raise BrowserError("页面未就绪（找不到 chat prompt）")

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

    def _stream_locked(self, body: dict, cancel: threading.Event,
                       deadline: float, trace: Callable | None) -> Iterator[str]:
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
                    raise BrowserHTTPError(status if status > 0 else 502,
                                           f"上游 {status}: {data[:500] or state.get('error')}")
                if state.get("error"):
                    raise BrowserError(f"上游响应读取失败: {state['error']}")
                if data:
                    if status != 200:
                        raise BrowserError("上游数据缺少成功响应状态")
                    last_data = time.monotonic()
                    yield data
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
    """多个 Playground 标签页，每个标签一个 BrowserUpstream，支持并发。"""

    def __init__(
        self,
        port: int = 9222,
        size: int = 3,
        base_url: str = DEFAULT_BASE_URL,
        prefix: str = "kpool",
    ) -> None:
        self.port = port
        self.size = max(1, size)
        self.base_url = base_url
        self.prefix = prefix
        self._ups: list[BrowserUpstream] = []
        self._queue: queue.Queue[BrowserUpstream] = queue.Queue()
        self._turns: dict[str, int] = {}
        self._refresh: set[str] = set()
        self._ready = False
        self._lock = threading.Lock()

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
    def _call(ws, mid: int, method: str, params: dict | None = None):
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise BrowserError(str(msg["error"]))
                return msg.get("result", {})

    def _navigate(self, ws_url: str, url: str) -> None:
        ws = websocket.create_connection(ws_url, timeout=5)
        try:
            self._call(ws, 1, "Page.enable")
            self._call(ws, 2, "Page.navigate", {"url": url})
        finally:
            ws.close()

    def _activate_wait(self, ws_url: str, cancel: threading.Event,
                       deadline: float) -> None:
        ws = websocket.create_connection(ws_url, timeout=5)
        try:
            self._call(ws, 1, "Page.bringToFront")
            self._call(ws, 2, "Runtime.enable")
            expr = r"""
(() => ({
  ready: document.readyState,
  ta: !!document.querySelector('textarea[aria-label="chat prompt"]'),
  send: !!document.querySelector('button[aria-label="Send"]'),
  hc: typeof hcaptcha,
  body: document.body ? document.body.innerText.length : 0
}))()
"""
            ready_deadline = min(deadline, time.monotonic() + 60)
            while time.monotonic() < ready_deadline:
                _check(cancel, deadline)
                res = self._call(ws, 3, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
                value = res.get("result", {}).get("value") or {}
                if (
                    value.get("ready") == "complete"
                    and value.get("ta")
                    and value.get("send")
                    and value.get("hc") == "object"
                    and value.get("body", 0) > 500
                ):
                    _pause(cancel, deadline, 0.5)
                    return
                _pause(cancel, deadline, 0.2)
            raise BrowserError("池标签页未就绪（textarea/Send/hcaptcha 未出现）")
        finally:
            ws.close()

    def _ensure(self, cancel: threading.Event, deadline: float) -> None:
        if self._ready:
            return
        while not self._lock.acquire(timeout=0.05):
            _check(cancel, deadline)
        try:
            if self._ready:
                return
            pages = self._list_pages()
            upstreams = []
            for n in range(1, self.size + 1):
                _check(cancel, deadline)
                hint = f"{self.prefix}={n}"
                page = next((p for p in pages if _matches_hint(p.get("url") or "", hint)), None)
                if page is None:
                    tab = self._new_blank()
                    self._navigate(tab["webSocketDebuggerUrl"], f"{self.base_url}?{hint}")
                    page = tab
                self._activate_wait(page["webSocketDebuggerUrl"], cancel, deadline)
                upstream = BrowserUpstream(port=self.port, target_hint=hint)
                upstreams.append(upstream)
            self._ups = upstreams
            for upstream in upstreams:
                self._queue.put(upstream)
            self._ready = True
        finally:
            self._lock.release()

    def _refresh_tab(self, upstream: BrowserUpstream, cancel: threading.Event,
                     deadline: float) -> None:
        pages = self._list_pages()
        page = next((p for p in pages if _matches_hint(p.get("url") or "", upstream.hint)), None)
        if page is None:
            raise BrowserError(f"找不到标签页 {upstream.hint}")
        upstream.close()
        self._navigate(page["webSocketDebuggerUrl"], f"{self.base_url}?{upstream.hint}")
        self._activate_wait(page["webSocketDebuggerUrl"], cancel, deadline)
        upstream.needs_refresh = False

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
            try:
                upstream = self._queue.get(timeout=0.05)
                break
            except queue.Empty:
                if time.monotonic() >= queued_until:
                    raise BrowserError(f"浏览器池排队超时（>{QUEUE_TIMEOUT:.0f}s），所有标签页都在忙")
        hint = upstream.hint
        try:
            if hint in self._refresh or upstream.needs_refresh:
                upstream.needs_refresh = True
                print(f"[pool] refresh tab={hint} before next request", flush=True)
                self._refresh_tab(upstream, cancel, deadline)
                self._refresh.discard(hint)
                self._turns[hint] = 0
            yield from upstream.stream(body, max(0, deadline - time.monotonic()), cancel, trace)
        except BaseException:
            upstream.needs_refresh = True
            raise
        finally:
            # 所有 CDP 操作/计数必须在归还标签页之前完成。
            try:
                turns = self._turns.get(hint, 0) + 1
                self._turns[hint] = turns
                if not cancel.is_set() and not upstream.needs_refresh:
                    text_len = upstream.page_text_len()
                    if turns >= TAB_MAX_TURNS or text_len >= TAB_MAX_TEXT_LEN:
                        self._refresh.add(hint)
                        print(f"[pool] mark refresh tab={hint} turns={turns} text_len={text_len}", flush=True)
            except Exception:
                upstream.needs_refresh = True
            if trace:
                trace("browser_pool_release", tab_id=hint, needs_refresh=upstream.needs_refresh)
            self._queue.put(upstream)

    def close(self) -> None:
        for upstream in self._ups:
            upstream.close()
        self._ups.clear()
        self._queue = queue.Queue()
        self._turns.clear()
        self._refresh.clear()
        self._ready = False
