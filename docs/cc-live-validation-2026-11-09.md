# Claude Code live investigation, 2026-11-09

All observation times below are Asia/Shanghai (UTC+08:00). Raw runtime evidence
is kept locally in ignored `logs/`; it is not a portable test fixture.

## Initial incident

The running service was launched by `start-bridge.sh`, listening on port 8083
and using five Brave tabs on CDP port 9225. Claude Code 2.1.319 was analysing
`/home/zhyhang/code/person/taomni`, with two background analysis agents.

- At 22:57:01, browser requests `cfea693e` and `ff544a86` failed with
  `upstream 500: None`. Neither had delivered upstream content.
- At 23:02:24–25, requests `929e8ff1`, `0a95872f` and `8ea44fc7` failed with
  `TypeError: network error`. All three had delivered content earlier.
  The main CC transcript recorded “Server error mid-response” at 23:02:24.891.
- The user resumed the main session at 23:03:13. Later transcript timestamps,
  increasing completion-token counts and subsequent tool results confirmed
  continued work by the main session and both background agents.

The simultaneous network failures are observed upstream reader failures; their
underlying network/provider cause is not established. Replaying already
delivered thinking, text or tools would violate stream semantics, so that policy
remains an explicit failure followed by client continuation.

## Confirmed bridge defects and fixes

`fetch()` makes a response status available before the cloned body's reader
necessarily produces data. The CDP loop previously raised immediately on a
non-200 status and aborted the reader. An error header followed by delayed body
chunks therefore became `500: None`, losing both diagnostics and the detail
required by the existing narrowly scoped NVIDIA pool-timeout retry policy.

`tests/test_browser_http_errors.py` first reproduced the lost body against the
old implementation. The fix collects at most 8192 characters for at most five
seconds, respecting cancellation and the existing total request deadline.
Collection never emits an HTTP error body as successful model output. The
bounded message survives the thread/async boundary so a pool-timeout marker
after a long prefix can still be classified. Other HTTP failures retain their
status; missing, oversized, stalled and failed bodies are explicit.

The diagnostic `tool_calls` counter also counted each streamed argument fragment
as a separate invocation. It now counts distinct upstream invocation indexes.
Failure metadata includes status, delivered-content state, counts and usage,
which helps distinguish pre-content retries from mid-response failures.

Validation: the nine new CDP/error-boundary tests passed, as did all 129 tests
in the explicit offline suite (request conversion, pool/recovery, HTTP delivery,
Responses, retry policy, agent compatibility and HTTP error collection).

An opt-in probe, `tests/probe_browser_http_errors.py`, also exercised the actual
running Brave through CDP. Its isolated local page supplies HTTP 500 headers
before two delayed error chunks, then a successful SSE response on retry.
Both Messages and Responses completed with exactly two fixture requests and
the expected success terminal event. This validates real browser reader timing
without spending NVIDIA requests or touching production tabs.

Two live scripts previously issued network requests at import time. An initial
discovery attempt exposed that behavior and was stopped; its separate `ktest`
page was closed. Both scripts now have explicit `main` entry points, so offline
test discovery does not import a live workload. Standard discovery subsequently
passed all 129 tests.

The old monitor discarded a partially written JSON line, and did not recover
from a shortened or replaced log. It now tails byte offsets while retaining
incomplete records, reports pool states, and offers a snapshot with request IDs,
ages, activity timestamps, counts, usage and recent errors. Three focused tests
cover split UTF-8 writes, truncation/replacement and retry/failure tracking.

## Supervised implementation workload

A separate CC session was started through the bridge, with no background agents,
to implement Notes workspace search clearing and investigate one reproducible
Notes bug. It uses branch `feat/cc-bridge-notes-search-20261109` in taomni.
The review and hosted execution results will be recorded after completion.

## Further live evidence and HTTP recovery

A real CC smoke session through the patched service called Bash and Read and
returned the independently checked SHA-256 and README title. It completed three
turns in approximately 133 seconds. The service on port 8084 uses a separate
`kcanary` pool namespace; the original service and its active sessions remain
on port 8083 with `kpool` tabs.

A deliberate interrupt of the supervised implementation session at 23:27:11
produced a confirmed browser abort and `browser_cancelled` within approximately
55 milliseconds. The same CC session then resumed through the patched service.
Repeated analysis of a small test led to a later, fresh focused session with
medium effort and a smaller context. That session inherits the implementation
branch and test changes, not concurrent ownership of the product files.

At 23:46:34 the patched service recorded an HTTP 500 with no error body within
the five-second collection window. CC automatically retried the same history
as a non-streaming request. This establishes a remaining pre-content recovery
case even when the NVIDIA-specific pool marker is unavailable.

The bridge now additionally retries confirmed HTTP 500/502/503/504 once before
content delivery. The default backoff is 2–2.4 seconds; it retains the same SSE
connection, payload and total deadline. HTTP provenance is carried separately
from normalized API error status, preventing a failed fetch or HTTP-200 error
event from being mistaken for a received HTTP 5xx response. The specific pool
timeout policy keeps its separate budget and priority.

All 138 offline tests pass. The real-CDP fixture covers both specific pool and
generic server failures through both API protocols: four cases, two fixture
requests each, all successful. Network failures after delivered content still
end explicitly; the underlying provider/network cause remains unestablished.
