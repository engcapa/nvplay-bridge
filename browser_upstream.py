"""browser_upstream.py — 通过 CDP 驱动 Playground 页面完成一次推理请求。

原理：
  1. 页面自己点一次“发送”，由页面 JS 现场签发 nv-captcha-token；
  2. 我们在 fetch 层把请求体替换成目标 body（可含 tools）；
  3. 克隆响应流，读完整 SSE 文本返回。

前提：Chrome 以 --remote-debugging-port=9222 启动，并打开 Playground 页面。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.request

import websocket

DEFAULT_HINT = "kimi-k3_1-pro/playground"
DEFAULT_BASE_URL = "https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground"
QUEUE_TIMEOUT = float(os.getenv("BROWSER_QUEUE_TIMEOUT", "120"))
TAB_MAX_TURNS = int(os.getenv("BROWSER_TAB_MAX_TURNS", "50"))
TAB_MAX_TEXT_LEN = int(os.getenv("BROWSER_TAB_MAX_TEXT_LEN", "500000"))

PATCH_JS = r"""
(() => {
  if (window.__bridgeInstalled) return 'already';
  window.__bridgeInstalled = true;
  window.__bridge = { body: null, text: '', status: 0, done: false, error: null, ct: null };
  const prev = window.fetch.bind(window);
  window.fetch = function (input, init) {
    let url = typeof input === 'string' ? input
      : (input instanceof Request ? input.url : String(input));
    const b = window.__bridge;
    if (!url.includes('/v2/predict/models/') || !b || !b.body) return prev(input, init);

    b.text = ''; b.done = false; b.status = 0; b.error = null;
    let req;
    try {
      if (input instanceof Request) {
        const h = new Headers(input.headers);
        h.delete('host'); h.delete('content-length');
        req = new Request(input.url, {
          method: input.method, headers: h, body: b.body,
          credentials: 'include', mode: 'cors'
        });
      } else {
        const h = new Headers((init && init.headers) || {});
        h.delete('host'); h.delete('content-length');
        req = new Request(url, {
          method: (init && init.method) || 'POST', headers: h, body: b.body,
          credentials: 'include', mode: 'cors'
        });
      }
    } catch (e) { b.error = String(e); return prev(input, init); }

    const p = prev(req);
    p.then(async r => {
      b.status = r.status;
      b.ct = r.headers.get('content-type');
      try {
        const reader = r.clone().body.getReader();
        const dec = new TextDecoder();
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          b.text += dec.decode(value, { stream: true });
        }
        b.text += dec.decode();
      } catch (e) { b.error = String(e); }
      b.done = true;
    }).catch(e => { b.status = -1; b.error = String(e); b.done = true; });
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

    # ---------------- CDP 基础 ----------------
    def _target(self) -> dict:
        try:
            with urllib.request.urlopen(f"{self.base}/json/list", timeout=5) as resp:
                targets = json.load(resp)
        except OSError as exc:
            raise BrowserError(
                f"CDP 未就绪（{self.base}）。请用 --remote-debugging-port={self.base.rsplit(':', 1)[-1]} 启动 Chrome 并打开 Playground。"
            ) from exc
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        for t in pages:
            if self.hint in (t.get("url") or ""):
                return t
        if pages:
            return pages[0]
        raise BrowserError("没有可用的页面 target，请先在 Chrome 打开 Playground")

    def _connect(self) -> None:
        if self.ws is not None:
            return
        target = self._target()
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=20)
        self._call("Runtime.enable")

    def _call(self, method: str, params: dict | None = None, timeout: float = 60.0):
        if self.ws is None:
            self._connect()
        self.seq += 1
        mid = self.seq
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.3, deadline - time.time()))
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise BrowserError(str(msg["error"]))
                return msg.get("result", {})
        raise BrowserError(f"CDP timeout: {method}")

    def _eval(self, expr: str, timeout: float = 60.0):
        res = self._call(
            "Runtime.evaluate",
            {"expression": expr, "awaitPromise": True, "returnByValue": True},
            timeout,
        )
        if res.get("exceptionDetails"):
            raise BrowserError(json.dumps(res["exceptionDetails"])[:800])
        return res.get("result", {}).get("value")

    # ---------------- 公开接口 ----------------
    def _prepare_page(self) -> None:
        """关闭 cookie/免责声明弹窗，并等待页面重载稳定。

        首次使用全新 profile 时，点击 “Acknowledge & Continue” 可能触发整页重载；
        必须在重载完成后再注入 fetch 补丁，否则补丁会随旧上下文一起丢失。
        """
        for _ in range(4):
            self._wait_ready()
            try:
                res = self._eval(DISMISS_JS, timeout=20) or {}
            except BrowserError:
                res = {}
            if res.get("ack") or res.get("consent"):
                time.sleep(2.5)
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
            time.sleep(1.5)
        raise BrowserError("免责声明弹窗未关闭")

    def _wait_ready(self) -> None:
        for _ in range(3):
            try:
                if self._eval(WAIT_JS, timeout=60) == "ready":
                    return
            except BrowserError:
                pass
            time.sleep(2)
        raise BrowserError("页面未就绪（找不到 chat prompt）")

    def page_text_len(self) -> int:
        try:
            value = self._eval("document.body ? document.body.innerText.length : 0", timeout=10)
            return int(value or 0)
        except Exception:  # noqa: BLE001
            return 0

    def generate(self, body: dict, timeout: float = 180.0) -> str:
        if not self._lock.acquire(timeout=QUEUE_TIMEOUT):
            raise BrowserError(
                f"浏览器桥排队超时（>{QUEUE_TIMEOUT:.0f}s），前一个请求可能卡住；请检查页面或重启 bridge"
            )
        try:
            return self._generate_locked(body, timeout)
        finally:
            self._lock.release()

    def _generate_locked(self, body: dict, timeout: float = 180.0) -> str:
        self._connect()
        self._prepare_page()
        if self._eval(PATCH_JS) is None:
            raise BrowserError("注入 fetch 补丁失败")
        self._eval(
            "window.__bridge.body = " + json.dumps(json.dumps(body, ensure_ascii=False))
        )
        self._eval("window.__bridge.text=''; window.__bridge.done=false; window.__bridge.status=0; window.__bridge.error=null;")

        # 页面在每次发送后会清空输入并把 Send 置灰；不要死等按钮，
        # 直接 trigger（它会重新填入文字并触发 input），disabled 时重试。
        trigger = None
        deadline = time.time() + 30
        while time.time() < deadline:
            trigger = self._eval(TRIGGER_JS, timeout=30)
            if trigger != "send disabled":
                break
            time.sleep(1)
        if trigger != "clicked":
            raise BrowserError(f"触发发送失败: {trigger}")

        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self._eval(
                "({done:!!(window.__bridge&&window.__bridge.done),"
                "status:window.__bridge&&window.__bridge.status,"
                "err:window.__bridge&&window.__bridge.error})"
            )
            if state.get("done"):
                if state.get("status") != 200:
                    text = self._eval("window.__bridge.text") or ""
                    status = int(state.get("status") or 502)
                    raise BrowserHTTPError(status, f"上游 {status}: {text[:500]}")
                text = self._eval("window.__bridge.text") or ""
                self._eval("window.__bridge.body = null")
                return text
            time.sleep(0.4)

        raise BrowserError("等待模型响应超时")

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
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=5) as resp:
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
        with urllib.request.urlopen(req, timeout=10) as resp:
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
        ws = websocket.create_connection(ws_url, timeout=15)
        try:
            self._call(ws, 1, "Page.enable")
            self._call(ws, 2, "Page.navigate", {"url": url})
        finally:
            ws.close()

    def _activate_wait(self, ws_url: str) -> None:
        ws = websocket.create_connection(ws_url, timeout=15)
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
            deadline = time.time() + 60
            while time.time() < deadline:
                res = self._call(ws, 3, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
                value = res.get("result", {}).get("value") or {}
                if (
                    value.get("ready") == "complete"
                    and value.get("ta")
                    and value.get("send")
                    and value.get("hc") == "object"
                    and value.get("body", 0) > 500
                ):
                    time.sleep(1)
                    return
                time.sleep(0.5)
            raise BrowserError("池标签页未就绪（textarea/Send/hcaptcha 未出现）")
        finally:
            ws.close()

    def _ensure(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            pages = self._list_pages()
            for n in range(1, self.size + 1):
                hint = f"{self.prefix}={n}"
                page = next((p for p in pages if hint in (p.get("url") or "")), None)
                if page is None:
                    tab = self._new_blank()
                    self._navigate(tab["webSocketDebuggerUrl"], f"{self.base_url}?{hint}")
                    page = tab
                self._activate_wait(page["webSocketDebuggerUrl"])
                upstream = BrowserUpstream(port=self.port, target_hint=hint)
                self._ups.append(upstream)
                self._queue.put(upstream)
            self._ready = True

    def _refresh_tab(self, upstream: BrowserUpstream) -> None:
        pages = self._list_pages()
        page = next((p for p in pages if upstream.hint in (p.get("url") or "")), None)
        if page is None:
            raise BrowserError(f"找不到标签页 {upstream.hint}")
        self._navigate(page["webSocketDebuggerUrl"], f"{self.base_url}?{upstream.hint}")
        self._activate_wait(page["webSocketDebuggerUrl"])
        upstream.close()

    def generate(self, body: dict, timeout: float = 180.0) -> str:
        self._ensure()
        try:
            upstream = self._queue.get(timeout=QUEUE_TIMEOUT)
        except queue.Empty as exc:
            raise BrowserError(
                f"浏览器池排队超时（>{QUEUE_TIMEOUT:.0f}s），所有标签页都在忙"
            ) from exc

        hint = upstream.hint
        if hint in self._refresh:
            self._refresh.discard(hint)
            self._turns[hint] = 0
            print(f"[pool] refresh tab={hint} before next request", flush=True)
            try:
                self._refresh_tab(upstream)
            except Exception as exc:  # noqa: BLE001
                print(f"[pool] refresh tab={hint} failed: {exc}", flush=True)

        try:
            text = upstream.generate(body, timeout)
        finally:
            self._queue.put(upstream)

        try:
            turns = self._turns.get(hint, 0) + 1
            self._turns[hint] = turns
            text_len = upstream.page_text_len()
            if turns >= TAB_MAX_TURNS or text_len >= TAB_MAX_TEXT_LEN:
                self._refresh.add(hint)
                reason = []
                if turns >= TAB_MAX_TURNS:
                    reason.append(f"turns={turns}>={TAB_MAX_TURNS}")
                if text_len >= TAB_MAX_TEXT_LEN:
                    reason.append(f"text_len={text_len}>={TAB_MAX_TEXT_LEN}")
                print(f"[pool] mark refresh tab={hint} {', '.join(reason)}", flush=True)
        except Exception:  # noqa: BLE001
            pass
        return text

    def close(self) -> None:
        for upstream in self._ups:
            upstream.close()
        self._ups.clear()
        self._ready = False
