# nvplay-bridge

Anthropic Messages and OpenAI Responses API bridge for **Kimi K3.1 Pro** running on the NVIDIA Build Playground, driven through a real Brave window over CDP. Supports Claude Code and Codex over HTTP streaming.

> Unofficial project. Not affiliated with NVIDIA, Moonshot AI, or Anthropic.

This project started as a local research shim and was cleaned up for publishing.

## Layout

```text
nvplay-bridge/
  nvplay_bridge.py       FastAPI app: Anthropic /v1/messages translation, SSE, retries, loop breaker
  browser_upstream.py    CDP browser controller and 5-tab pool
  browser_events.py      bounded thread-to-async stream and cancellation
  stream_support.py      incremental SSE parser and Anthropic content blocks
  responses_support.py   Responses request conversion, output items and bounded history
  responses_api.py       Responses HTTP/SSE routes and cancellation
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
- **Python 3.11 or newer**. Any offline `vendor/` bundle must match the Python
  version, OS and architecture in use; rebuild it when switching interpreters.
- Dependencies: `python -m pip install -r requirements.txt`
- Brave. The launcher detects an installed Brave and otherwise downloads the
  official portable build for the current OS/architecture into `.brave/`
- A Brave profile that has already passed the Playground hCaptcha once

Throughout this README, `$REPO` is your checkout and `$PY` is a Python 3.11+
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

1. checks the Python version (3.11 or newer) and creates `logs/`
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

On Linux, if Brave exits with a sandbox initialization error, the shell launcher
retries with `--no-sandbox` and prints a warning. This supports containers that
block user namespaces and portable builds whose SUID helper is not configured.
Browser output is saved to `logs/brave.log`; startup failures show its last lines.

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
| `BROWSER_FIRST_BYTE_TIMEOUT` | `BROWSER_TIMEOUT` (`3600`) | Seconds from send to first upstream data; the total request deadline still applies |
| `BROWSER_IDLE_TIMEOUT` | `90` | Max seconds without upstream data after generation starts |
| `BROWSER_POLL_INTERVAL` | `0.1` | CDP incremental drain interval in seconds |
| `BROWSER_HEARTBEAT_SEC` | `10` | SSE ping interval while generating; `0` disables |
| `BROWSER_QUEUE_TIMEOUT` | `120` | Max wait for a pool tab |
| `BROWSER_TAB_MAX_TURNS` | `50` | Refresh a tab after N requests |
| `BROWSER_TAB_MAX_TEXT_LEN` | `500000` | Refresh a tab when DOM text exceeds this |
| `BROWSER_TAB_READY_TIMEOUT` | `30` | Seconds per tab recovery attempt and request preparation |
| `BROWSER_TAB_RECOVERY_ATTEMPTS` | `2` | Recovery attempts before isolating a failed tab |
| `BROWSER_TAB_RECOVERY_BACKOFF` | `5` | Initial seconds before retrying an isolated tab; doubles to 60s |
| `BROWSER_POOL_TIMEOUT_RETRIES` | `2` | Additional attempts for NVIDIA HTTP 500 pool-acquire timeouts before content delivery; `0` disables |
| `BROWSER_POOL_TIMEOUT_BACKOFF` | `5` | Initial seconds between these attempts; doubles to 30s, plus up to 1s jitter |
| `BROWSER_HTTP_5XX_RETRIES` | `1` | Additional attempts for confirmed HTTP 500/502/503/504 before content delivery; `0` disables |
| `BROWSER_HTTP_5XX_BACKOFF` | `2` | Initial seconds between these attempts; doubles to 30s, plus up to 1s jitter |
| `PLACEHOLDER_RETRY` | `1` | Retry placeholder/empty upstream responses |
| `LOOP_BREAKER` | `0` | Optional reminder for three identical calls/results in the current user turn; never forces task completion |
| `HISTORY_REASONING` | `preserve` | `preserve` / `omit`: include or omit past assistant reasoning upstream; `omit` is experimental for the NVIDIA deployment |
| `BROWSER_DEBUG_LOG` | empty | JSONL metadata log path |
| `BROWSER_DEBUG_MESSAGES` | `0` | Log outbound message shapes |

## API behavior

- `GET /health`, `GET /v1/models`
- `POST /v1/messages`, `POST /v1/messages/count_tokens`
- `POST /v1/responses` (JSON or SSE), `GET/DELETE /v1/responses/{id}`
- Anthropic SSE: `message_start`, `content_block_*`, `message_delta`, `message_stop`
- `output_config.effort` maps directly to upstream `reasoning_effort`; `xhigh` → `max`
- If CC sends no thinking/effort, the bridge sets no `reasoning_effort`
- Missing `max_tokens` → `131072` (128K); larger values are capped
- Optional bridge extension: an explicitly supplied integer `seed` (including `0`) is forwarded upstream in both APIs; absent/null leaves it unset. A seed does not guarantee identical provider output.
- Assistant `thinking` blocks are passed back as `reasoning_content` with `HISTORY_REASONING=preserve` (default). Set `HISTORY_REASONING=omit` before starting the bridge to keep visible messages and tool calls/results while excluding old reasoning from subsequent upstream requests. Both policies still deliver newly generated thinking to the client; the setting does not alter effort or output budget. `/health` reports the active policy.
- Kimi's official K3.1 Pro API documentation requires preserving complete assistant messages, including reasoning, for multi-turn and tool use. The NVIDIA UI was observed to omit old reasoning in a local mocked follow-up, and one repair-task experiment succeeded with omission. This does not establish a general quality benefit; keep `preserve` as the default. See [history policy and evidence](docs/agent-compatibility.md).
- Tool invocation IDs are fresh across responses in Messages and Responses, for both SSE and JSON. Tool names, arguments and returned text are preserved.
- Invalid/non-object tool parameter JSON fails explicitly; Messages JSON aggregation does not silently replace malformed arguments with `{}`.
- Browser mode forwards thinking, text and the first tool's arguments as they arrive; interleaved additional tools are buffered and emitted as consecutive content blocks.
- Browser mode sends `event: ping` every 10s without downstream data, including while queuing and retrying.
- Placeholder/empty responses may retry once before streaming content is delivered. After thinking/text/tool content has been delivered, errors end the stream without replacing the answer. Non-streaming requests retain the empty-response retry.
- NVIDIA HTTP 500 errors containing `Pool#acquire(Duration) has been pending for more than the configured timeout` may retry twice before any thinking/text/tool content is delivered. The same downstream SSE connection stays open with heartbeats during the default 5–6s and 10–11s waits. Attempts preserve the payload and share the original total deadline; placeholder correction has a separate one-retry allowance. Exhaustion returns the original error. Metadata logs use `upstream_retry` and `upstream_retry_skipped` to identify retries and their limits. See [upstream retry design](docs/upstream-pool-timeout-retry.md).
- HTTP error headers may arrive before their body. Browser mode collects up to 8192 characters for at most five seconds within the original request deadline, so split NVIDIA error details can reach the retry classifier. Empty, stalled, failed and oversized error bodies remain explicit failures with their original HTTP status. `browser_http_error` records collection state; `browser_error` includes delivery state, counts and usage. Tool-call counts represent distinct invocation indexes, rather than streamed argument fragments.
- Other confirmed HTTP 500/502/503/504 responses may retry once before content delivery, including when the error body is unavailable. The default delay is 2–2.4 seconds with heartbeats on the original SSE connection. This budget is separate from the specific pool-timeout budget, and the specific policy takes priority. Network failures without a received HTTP response and error events inside an HTTP 200 stream are not classified as retryable HTTP statuses. Retries preserve the payload and total deadline; `upstream_retry.reason=upstream_http_5xx` identifies them. Already delivered content prevents replay.
- Client disconnects abort the matching browser fetch. Interrupted/failed tabs leave the available queue and recover in a dedicated worker before reuse.
- A preparation failure before arming/sending can switch to another healthy tab inside the original SSE connection, with heartbeats while waiting. Failures after triggering a send are not transparently replayed except for the explicit pool-acquire timeout and empty/placeholder policies above; an uncertain network failure before the first byte still fails immediately.
- Regular data prevents idle timeouts, but the 3600-second total limit still applies. The included Claude Code settings use `API_TIMEOUT_MS=3900000` (65 minutes) to allow time for cleanup; restart CC to load changed settings.
- 5-tab pool; recovery never navigates a busy tab, and healthy tabs remain usable while other tabs recover. `/health` includes pool states and recent recovery errors after pool initialization.
- The optional tool repetition reminder uses only completed calls in the supplied history since the latest user message. It does not count HTTP resends, share counters between sessions, or tell the agent to finish early. `Error:` and `<tool_use_error>` results are excluded; other tool errors have no universal encoding. It is disabled by default.

Readiness, recovery limits and offline regression coverage are described in
[browser pool recovery](docs/browser-pool-recovery.md).

## OpenAI Responses and Codex

Use `http://127.0.0.1:8083/v1` as an OpenAI SDK base URL. The local endpoint
does not require a key; SDKs that require one can use a placeholder:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8083/v1", api_key="local-only")
with client.responses.stream(
    model="kimi-k3.1-pro", input="Compute 17 + 25.",
    reasoning={"effort": "high"}, store=False,
) as stream:
    for event in stream:
        if event.type == "response.output_text.delta":
            print(event.delta, end="", flush=True)
```

[configs/codex-bridge.toml](configs/codex-bridge.toml) is an example for a
separate Codex configuration directory. It selects `wire_api="responses"`,
disables WebSocket transport and hosted web search, and points at the local
bridge. Existing user configuration is never edited by the supplied tests.
Default Codex requests can advertise hosted web search without blocking
ordinary messages or client tools. The bridge reports that capability as
unavailable and omits it from effective tools; explicitly requiring hosted
search returns an error. The example disables that unsupported capability.

Supported paths include developer instructions, explicit conversation history,
reasoning/text deltas, function and custom tools, namespaces, interleaved tool
arguments and tool result round trips. Stored response IDs support continuation
within this server process, subject to memory limits and expiry. Codex normally
uses `store=false` and resends explicit history. Upstream truncation emits
`response.incomplete`; broken streams emit `response.failed` without a false
completion. See [Responses compatibility](docs/responses-compatibility.md)
for supported fields, limits and validation results.

## Tests

```powershell
$env:PYTHONPATH = $REPO     # add ";$REPO\vendor" only if you have an offline bundle
& $PY tests\test_nvplay_bridge.py
& $PY tests\test_pool.py
& $PY tests\test_streaming.py
& $PY tests\test_pool_recovery.py
& $PY tests\test_responses.py
& $PY tests\test_upstream_retry.py
& $PY tests\test_agent_compatibility.py
& $PY tests\test_browser_http_errors.py
& $PY tests\test_monitor_bridge.py
```

The offline suite can also be discovered with
`PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py'`.
Live regression scripts send requests only when executed directly.

For supervised CC benchmark runs without Docker, see the
[native Terminal-Bench evaluation guide](docs/terminal-bench-native.md).
The adapted subset scores are separate from the official AA Intelligence Index.

For a deterministic check against a running Brave/CDP instance, run
`PYTHONPATH=. python3 tests/probe_browser_http_errors.py --cdp-port 9225`.
It opens an isolated local fixture page, verifies delayed/split HTTP error
collection and retry through Messages and Responses, then closes that page.
It sends no NVIDIA inference requests and does not use the production pool tabs.

`python3 tools/monitor_bridge.py --snapshot` reports recent active requests,
their age and last activity, token usage, terminal errors and pool health.
Use `--lookback 1800` to limit event/error history; active requests retain their
full lifetime, including requests with no recent progress. Use `--log PATH` for another metadata log,
or `--health-url http://127.0.0.1:8084/health` for another service instance.
The default follows new events for 90 seconds; `--duration 0` follows until
interrupted. The reader retains incomplete UTF-8/JSON lines and handles file
replacement or observed truncation without advancing past a partial record.

Live regression (needs Brave/CDP and a running bridge):

```powershell
& $PY tests\live_regression.py
```

Streaming-specific live checks start and stop their own temporary HTTP server
and require only Brave/CDP: `python tests/live_streaming.py`.

Responses E2E uses a temporary server and separate `responsee2e` browser tabs.
The `--codex` option runs the installed Codex with a temporary configuration
and workspace, checks actual file/tool execution, and verifies that the
existing Codex config and auth files retain their original hashes:

```powershell
& $PY tests\live_responses.py --codex
```

On Windows this opt-in CLI test uses the restricted MXC sandbox, selected
only in the temporary test home. It checks sandbox startup before inference;
it requires a Windows build that supports MXC. No administrator setup is run.

Reports are written to `logs/responses-e2e-report.json`; detailed diagnostics
and isolated Codex stdout/stderr also stay under `logs/`.

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
