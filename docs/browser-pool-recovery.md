# Browser pool readiness and recovery

## Observed failure

On 2026-11-08 a Claude Code `high` streaming request failed after approximately
60 seconds while recovering a pool tab. The page had the expected controls,
but hCaptcha was not initialized and the rendered body contained only 34
characters. Manually selecting the tab allowed initialization to continue.
The exact browser/page cause remains unproven.

The previous pool issued `Page.bringToFront` once, waited on the selected tab,
and returned its preparation failure to the caller. Claude Code then requested
`stream=false`; the upstream continued generating, but CC received the result
only after completion. Raising the generation timeout would not repair this
failure path.

## Scheduling and ownership

The default pool size is five in Python and both launchers. Initialization
starts one daemon maintenance thread on the first request. It probes existing
tabs before attempting any slow recovery, so each already healthy tab can
serve requests independently. Missing tabs are created during maintenance.

Only `ready` tabs enter the available queue. Borrowing a tab makes it `busy`;
the request keeps ownership through stream cleanup and threshold checks.
Cancellation, preparation failures, generation errors and refresh thresholds
remove that tab from circulation and mark it `pending`. Maintenance changes
`pending` to `recovering`, then to `ready` or `failed`. Failed tabs retry with
bounded exponential backoff. Newly pending tabs are considered before failed
tabs whose retry timer has expired, so an unhealthy first tab cannot keep
other tabs from warming up.

Only maintenance performs pool recovery. It never navigates a busy tab and
serializes navigation, activation and initialization. Known targets are reused
when a navigation fails, avoiding repeated creation of orphan blank tabs.
Pool shutdown cancels active leases and stops its recovery worker.

## Readiness and activation

Readiness requires a complete document, an editable prompt, a Send control,
and an initialized `hcaptcha.execute` API. Body text length is diagnostic,
not a prerequisite. Visibility, focus, discard status, outstanding page
requests and incompatible old patches are also inspected.

Recovery navigates an idle tab, resumes its lifecycle and enables focus
emulation when supported. It brings the tab to the front again every three
seconds while initialization remains incomplete, rather than assuming one
activation was sufficient. It confirms the navigation's loader ID before
accepting page readiness, so the previous document cannot pass the probe
while a reload is still pending. Unsupported optional CDP commands are logged.
Cookie/disclaimer handling and patch installation finish before a recovered
tab re-enters circulation. No diagnostic or warmup path sends a model prompt.

Each recovery attempt has `BROWSER_TAB_READY_TIMEOUT=30` seconds and there are
`BROWSER_TAB_RECOVERY_ATTEMPTS=2` attempts per recovery cycle. Failed cycles
back off from `BROWSER_TAB_RECOVERY_BACKOFF=5` seconds to at most 60 seconds.
Requests wait at most `BROWSER_QUEUE_TIMEOUT=120` seconds for a usable tab,
including switches after preparation failures. Request preparation has the
same 30-second phase limit. The 3600-second total request limit and existing
generation first-byte/idle limits remain in force.

The PowerShell and Bash launchers add:

```text
--disable-background-timer-throttling
--disable-renderer-backgrounding
--disable-backgrounding-occluded-windows
```

These flags affect newly launched Brave processes. Launchers inspect reused
process arguments and report missing flags without restarting that browser.
Their effectiveness for the observed initialization failure still needs a
manual test with the updated browser launch. They do not remove the need for
page readiness checks or satisfy a captcha challenge.

## Streaming and retry boundary

`BrowserPreparationError` is raised only for failures before arming the
fetch replacement or clicking Send. The pool can quarantine that tab and
try another within the same client connection. Existing `message_start`
and pings remain valid; preparation failures do not emit an SSE error while
another healthy tab can be obtained within the request's waiting budget.
Each lease quickly probes the page first; a tab that became unready while
idle is isolated immediately instead of waiting on it inside that request.

After the trigger stage begins, errors propagate and are never retried by
the pool. This includes an ambiguous click result, a successful send with no
first byte yet, a reader failure and a partial response. The existing
placeholder/empty-response policy is separate and unchanged.

Recovery belongs to the pool, not to an individual waiting client. Cancelling
a waiting request does not cancel maintenance needed by other requests. A
busy request still cancels its matching fetch before relinquishing ownership.
An incoming `stream=false` request continues to use the non-streaming API;
the bridge does not rewrite that client choice.

## Observability and verification

`/health` includes configured pool size, per-tab states and last recovery
errors after initialization. JSONL diagnostics include `pool_probe`,
`pool_activation`, `pool_page_ready`, `pool_recovery_start`,
`pool_recovery_error`, `pool_ready` and request-scoped
`browser_prepare_retry` events. Readiness failures include the final observed
page state without logging prompts, tokens or response content.

Offline tests in `tests/test_pool_recovery.py` cover partial startup, isolated
failures, bounded recovery/backoff, missing-tab recreation, concurrent leases,
waiter cancellation, readiness diagnostics, safe preparation failover and
preservation of a single SSE stream with heartbeats. Existing framing,
incremental HTTP delivery, cancellation and no-replay regressions also run.

No production service or browser restart is required for these offline tests.
Manual validation of the new launch flags, background initialization, window
occlusion and real CC/pi concurrency remains for the next scheduled deployment.
