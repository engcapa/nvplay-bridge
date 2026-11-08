# nvplay-bridge

Anthropic Messages API–compatible bridge for **Kimi K3.1 Pro** running on the NVIDIA Build Playground, driven through a real Brave/Chromium page over CDP.

> Unofficial project. Not affiliated with NVIDIA, Moonshot AI, or Anthropic.

This project started as a local research shim and was cleaned up for publishing.

## Layout

```text
nvplay-bridge/
  nvplay_bridge.py       FastAPI app: Anthropic /v1/messages translation, SSE, retries, loop breaker
  browser_upstream.py    CDP browser controller and 3-tab pool
  configs/               Claude Code settings and empty MCP config
  tests/                 unit, pool, tab-refresh and live regression scripts
  tools/                 monitors and global-CC setup helper
  docs/                  regression and effort analysis reports
  logs/                  runtime logs (not committed)
  start-bridge.ps1       one-command launcher: deps, browser, bridge
  requirements.txt       pinned dependencies
```

## Requirements

- Windows + PowerShell 7
- **Python 3.12** — the pinned `pydantic-core` build is CPython 3.12 only
  (`_pydantic_core.cp312-win_amd64.pyd`). A 3.13 interpreter fails at import
  time with `ImportError: cannot import name 'ArgsKwargs' from 'pydantic_core._pydantic_core'`.
- Dependencies: `python -m pip install -r requirements.txt`
- Brave or any Chromium with CDP enabled
- A browser profile that has already passed the Playground hCaptcha once

Throughout this README, `$REPO` is your checkout and `$PY` is a Python 3.12
interpreter:

```powershell
$REPO = "C:\path\to\nvplay-bridge"
$PY   = "C:\path\to\python312\python.exe"
& $PY -c "import fastapi, httpx, websocket; print('ok')"
```

## Quick start

```powershell
.\start-bridge.ps1 -Venv
```

The script creates `.venv` (optional), runs `pip install -r requirements.txt`, starts
Brave with CDP on port 9225, then runs the bridge on `127.0.0.1:8083` in the foreground.

| Switch | Meaning |
|---|---|
| `-Venv` | create and use `.venv` in the repository root |
| `-BravePath <path>` | braves/chrome executable; auto-detected when omitted |
| `-CdpPort <port>` | CDP port (default 9225) |
| `-BridgePort <port>` | bridge port (default 8083) |
| `-PoolSize <n>` | number of Playground tabs (default 3) |
| `-SkipPip` | do not install dependencies |
| `-SkipBrave` | do not start the browser |

Run `Get-Help .\start-bridge.ps1 -Detailed` for the full description. If pip needs a
proxy, set `HTTPS_PROXY`/`HTTP_PROXY` before invoking the script.

## 1. Start Brave

```powershell
& "C:\path\to\brave.exe" `
  --user-data-dir="$env:TEMP\brave-cdp" `
  --remote-debugging-port=9225 `
  --remote-allow-origins=* `
  --no-first-run --no-default-browser-check --disable-extensions `
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
$env:BROWSER_POOL_SIZE = "3"
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
| `BROWSER_POOL_SIZE` | `3` | Number of Playground tabs |
| `BROWSER_TIMEOUT` | `600` | Per-request upstream timeout |
| `BROWSER_HEARTBEAT_SEC` | `10` | SSE ping interval while generating; `0` disables |
| `BROWSER_QUEUE_TIMEOUT` | `120` | Max wait for a pool tab |
| `BROWSER_TAB_MAX_TURNS` | `50` | Refresh a tab after N requests |
| `BROWSER_TAB_MAX_TEXT_LEN` | `500000` | Refresh a tab when DOM text exceeds this |
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
- Browser mode sends `event: ping` every 10s while waiting, so CC's stream idle timer does not fire
- 3-tab pool; repeated tool calls are broken after a session-level repeat threshold

## Tests

```powershell
$env:PYTHONPATH = $REPO     # add ";$REPO\vendor" only if you have an offline bundle
& $PY tests\test_nvplay_bridge.py
& $PY tests\test_pool.py
```

Live regression (needs Brave/CDP and a running bridge):

```powershell
& $PY tests\live_regression.py
```

Tab refresh integration test (uses a temporary `?ktest=1` tab):

```powershell
& $PY tests\test_tab_threshold.py
```

## Known limitations

- Browser bridge depends on the Playground page, hCaptcha/WAF warm state, and a live Brave window.
- The bridge buffers upstream generation; heartbeats keep the client stream alive but do not stream partial model output.
- Claude Code background subagents can loop on `SubagentHandback` when the subagent session is not in auto permission mode. Prefer main-thread work or auto-mode subagents.
- Not an official API; upstream HTML/API changes can break it.

## Logs

`logs/bridge-debug.jsonl` records request/payload/browser events. See `docs/regression-report.md` for the latest test results.
