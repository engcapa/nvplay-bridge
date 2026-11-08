# nvplay-bridge

Anthropic Messages API–compatible bridge for **Kimi K3.1 Pro** running on the NVIDIA Build Playground, driven through a real Brave window over CDP.

> Unofficial project. Not affiliated with NVIDIA, Moonshot AI, or Anthropic.

This project started as a local research shim and was cleaned up for publishing.

## Layout

```text
nvplay-bridge/
  nvplay_bridge.py       FastAPI app: Anthropic /v1/messages translation, SSE, retries, loop breaker
  browser_upstream.py    CDP browser controller and 5-tab pool
  browser_events.py      bounded thread-to-async stream and cancellation
  stream_support.py      incremental SSE parser and Anthropic content blocks
  configs/               Claude Code settings and empty MCP config
  tests/                 unit, pool, tab-refresh and live regression scripts
  tools/                 monitors and global-CC setup helper
  docs/                  regression and effort analysis reports
  logs/                  runtime logs (not committed)
  start-bridge.ps1       Windows launcher: deps, Brave (auto-download), bridge
  start-bridge.sh        Linux / macOS launcher with the same steps
  requirements.txt       pinned dependencies
```

## Requirements

- Windows 11 (PowerShell 7), or Linux / macOS (bash)
- **Python 3.12** — the pinned `pydantic-core` build is CPython 3.12 only
  (`_pydantic_core.cp312-win_amd64.pyd`). A 3.13 interpreter fails at import
  time with `ImportError: cannot import name 'ArgsKwargs' from 'pydantic_core._pydantic_core'`.
- Dependencies: `python -m pip install -r requirements.txt`
- Brave. The launcher detects an installed Brave and otherwise downloads the
  official portable build for the current OS/architecture into `.brave/`
- A Brave profile that has already passed the Playground hCaptcha once

Throughout this README, `$REPO` is your checkout and `$PY` is a Python 3.12
interpreter:

```powershell
$REPO = "C:\path\to\nvplay-bridge"
$PY   = "C:\path\to\python312\python.exe"
& $PY -c "import fastapi, httpx, websocket; print('ok')"
```

## Quick start

```powershell
.\start-bridge.ps1          # Windows
```

```bash
chmod +x start-bridge.sh    # Linux / macOS, once
./start-bridge.sh
```

All settings live in the config block at the top of the script: `$Proxy` (empty
string = direct connection), `$Python`, `$CdpPort`, `$BridgePort`, `$PoolSize`,
`$BravePath`, `$BraveDir`, `$RequestTimeout` (3600 seconds). The shell launcher
uses the equivalent uppercase variables.

What it does:

1. checks the Python version (3.12 is required by the pinned wheels) and creates `logs/`
2. runs `pip install -r requirements.txt` (skipped when an offline `vendor/`
   bundle is present); `$Proxy` is passed to pip and to the Brave download only,
   it is not exported to the bridge process
3. reuses a browser already listening on the CDP port; otherwise finds an
   installed Brave, and if there is none, downloads the official portable build
   for the current OS/architecture into `.brave/` and extracts it; new browser
   processes disable background timer, renderer and occluded-window throttling
4. runs the bridge on `127.0.0.1:8083` in the foreground (`Ctrl+C` to stop)

The first run on a machine without Brave downloads ~200–300 MB into `.brave/`
(git-ignored). Later runs reuse it.

The pool defaults to five tabs. It warms tabs on the first request and serves
each healthy tab as soon as it is ready. Reusing an existing Brave process
does not change its launch arguments; the launcher reports missing background
flags. Apply those flags on the next planned browser restart.

## 1. Start Brave

`start-bridge.ps1` does this for you (and picks the right profile directory for
each OS). To run it by hand on Windows:

```powershell
& "C:\path\to\brave.exe" `
  --user-data-dir="$env:TEMP\brave-cdp" `
  --remote-debugging-port=9225 `
  --remote-allow-origins=* `
  --no-first-run --no-default-browser-check --disable-extensions `
  --disable-background-timer-throttling --disable-renderer-backgrounding `
  --disable-backgrounding-occluded-windows `
  --window-size=1280,900 `
  "https://build.nvidia.com/moonshotai/kimi-k3_1-pro/playground"
```

Wait until the Playground page is interactive. If hCaptcha challenges a fresh profile, send one message manually once to warm it.

Close every `brave.exe` that uses `%TEMP%\brave-cdp` before starting, or the profile stays locked and `--remote-debugging-port` silently does not open. Confirm the port before moving on:

```powershell
Invoke-RestMethod http://127.0.0.1:9225/json/version
```

## 2. Start the bridge

```powershell
$env:PYTHONPATH = $REPO     # add ";$REPO\vendor" only if you have an offline bundle
$env:UPSTREAM_KIND = "browser"      # required — defaults to "openai" and the bridge will not work without it
$env:BROWSER_CDP_PORT = "9225"
$env:BROWSER_POOL_SIZE = "5"
$env:BROWSER_HEARTBEAT_SEC = "10"
$env:BROWSER_DEBUG_LOG = "$REPO\logs\bridge-debug.jsonl"

& $PY -m uvicorn nvplay_bridge:app `
  --app-dir $REPO `
  --host 127.0.0.1 --port 8083
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8083/health
```

## 3. Claude Code

Use a settings file that points at the bridge:

```powershell
claude --settings "$REPO\configs\settings-kimi.json" `
  --model kimi-k3.1-pro
```

Or set the global config with `tools/set_global_cc.py`.

The bridge accepts only Kimi model aliases; other model IDs return `404 not_found_error`. It always sends `moonshotai/kimi-k3.1-pro` upstream. Because of that allowlist, every `configs/*.json` must name a Kimi alias — configs that still ask for `sonnet`/`haiku` are rejected.

| Settings file | Effort sent to upstream | Notes |
|---|---|---|
| `configs/settings-kimi.json` | `max` (via `CLAUDE_CODE_EXTRA_BODY`) | recommended default |
| `configs/settings-kimi-high.json` | `high` | same setup, lower effort |
| `configs/settings-bridge.json` | none forced | maps only what CC itself sends |
| `configs/settings-effort-max.json` | `max` | same result as `settings-kimi.json`; kept from the effort experiments |

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `UPSTREAM_KIND` | `openai` | `openai` / `nvcf` / `browser`; must be `browser` for this setup |
| `UPSTREAM_MODEL` | `moonshotai/kimi-k3.1-pro` | Real upstream model |
| `ALLOWED_MODELS` | Kimi aliases | Comma-separated request model allowlist |
| `BROWSER_MAX_TOKENS` | `131072` | Max output tokens (128K) in browser mode |
| `NVCF_MAX_TOKENS` | `131072` | Max output tokens (128K) in NVCF mode |
| `MAX_OUTPUT_TOKENS` | `131072` | Max output tokens (128K) in openai mode |
| `BROWSER_CDP_PORT` | `9222` | CDP port |
| `BROWSER_POOL_SIZE` | `5` | Number of Playground tabs |
| `BROWSER_TIMEOUT` | `3600` | Total request seconds, including queue, preparation and retries |
| `BROWSER_FIRST_BYTE_TIMEOUT` | `120` | Seconds from send to first upstream data |
| `BROWSER_IDLE_TIMEOUT` | `90` | Max seconds without upstream data after generation starts |
| `BROWSER_POLL_INTERVAL` | `0.1` | CDP incremental drain interval in seconds |
| `BROWSER_HEARTBEAT_SEC` | `10` | SSE ping interval while generating; `0` disables |
| `BROWSER_QUEUE_TIMEOUT` | `120` | Max wait for a pool tab |
| `BROWSER_TAB_MAX_TURNS` | `50` | Refresh a tab after N requests |
| `BROWSER_TAB_MAX_TEXT_LEN` | `500000` | Refresh a tab when DOM text exceeds this |
| `BROWSER_TAB_READY_TIMEOUT` | `30` | Seconds per tab recovery attempt and request preparation |
| `BROWSER_TAB_RECOVERY_ATTEMPTS` | `2` | Recovery attempts before isolating a failed tab |
| `BROWSER_TAB_RECOVERY_BACKOFF` | `5` | Initial seconds before retrying an isolated tab; doubles to 60s |
| `PLACEHOLDER_RETRY` | `1` | Retry placeholder/empty upstream responses |
| `LOOP_BREAKER` | `1` | Stop repeated identical tool calls |
| `BROWSER_DEBUG_LOG` | empty | JSONL metadata log path |
| `BROWSER_DEBUG_MESSAGES` | `0` | Log outbound message shapes |

## API behavior

- `GET /health`, `GET /v1/models`
- `POST /v1/messages`, `POST /v1/messages/count_tokens`
- Anthropic SSE: `message_start`, `content_block_*`, `message_delta`, `message_stop`
- `output_config.effort` maps directly to upstream `reasoning_effort`; `xhigh` → `max`
- If CC sends no thinking/effort, the bridge sets no `reasoning_effort`
- Missing `max_tokens` → `131072` (128K); larger values are capped
- Assistant `thinking` blocks are passed back as `reasoning_content`
- Browser mode forwards thinking, text and the first tool's arguments as they arrive; interleaved additional tools are buffered and emitted as consecutive content blocks.
- Browser mode sends `event: ping` every 10s without downstream data, including while queuing and retrying.
- Placeholder/empty responses may retry once before streaming content is delivered. After thinking/text/tool content has been delivered, errors end the stream without replacing the answer. Non-streaming requests retain the empty-response retry.
- Client disconnects abort the matching browser fetch. Interrupted/failed tabs leave the available queue and recover in a dedicated worker before reuse.
- A preparation failure before arming/sending can switch to another healthy tab inside the original SSE connection, with heartbeats while waiting. Failures after triggering a send are not transparently replayed, even if no first byte has arrived.
- Regular data prevents idle timeouts, but the 3600-second total limit still applies. The included Claude Code settings use `API_TIMEOUT_MS=3900000` (65 minutes) to allow time for cleanup; restart CC to load changed settings.
- 5-tab pool; recovery never navigates a busy tab, and healthy tabs remain usable while other tabs recover. `/health` includes pool states and recent recovery errors after pool initialization.
- Repeated tool calls are broken after a session-level repeat threshold

Readiness, recovery limits and offline regression coverage are described in
[browser pool recovery](docs/browser-pool-recovery.md).

## Tests

```powershell
$env:PYTHONPATH = $REPO     # add ";$REPO\vendor" only if you have an offline bundle
& $PY tests\test_nvplay_bridge.py
& $PY tests\test_pool.py
& $PY tests\test_streaming.py
& $PY tests\test_pool_recovery.py
```

Live regression (needs Brave/CDP and a running bridge):

```powershell
& $PY tests\live_regression.py
```

Streaming-specific live checks start and stop their own temporary HTTP server
and require only Brave/CDP: `python tests/live_streaming.py`.

Tab refresh integration test (uses a temporary `?ktest=1` tab):

```powershell
& $PY tests\test_tab_threshold.py
```

## Known limitations

- Browser bridge depends on the Playground page, hCaptcha/WAF warm state, and a live Brave window.
- CLI rendering of streamed thinking depends on the client. When upstream sends only thinking, receiving progress does not mean a final answer is ready.
- The first tool can stream arguments immediately. Additional interleaved tools wait until the active tool can close. Unexpected text/thinking interleaved after tools returns a protocol error.
- Claude Code background subagents can loop on `SubagentHandback` when the subagent session is not in auto permission mode. Prefer main-thread work or auto-mode subagents.
- Not an official API; upstream HTML/API changes can break it.

## Logs

`logs/bridge-debug.jsonl` records request/payload/browser events. See
[`docs/streaming-regression-report.md`](docs/streaming-regression-report.md) for
streaming validation and [`docs/regression-report.md`](docs/regression-report.md)
for the earlier regression results.
