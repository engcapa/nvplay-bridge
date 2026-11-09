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

Long-running live work exposed another snapshot defect: filtering log records
before reconstructing requests dropped a still-active request when its start
preceded `--lookback`. A real 60-second snapshot showed a busy pool and an empty
active list. Snapshots now replay the complete available log for lifetimes and
apply the window only to event counts and recent errors. A focused regression
covers both ongoing progress and a silent old request, without resurrecting
completed requests or old errors.

## Supervised implementation workload

A separate CC session was started through the bridge, with no background agents,
to implement Notes workspace search clearing and investigate one reproducible
Notes bug. It uses branch `feat/cc-bridge-notes-search-20261109` in taomni.

The selected defect is out-of-order persistence of Notes workspace status
filters. The native setter is asynchronous; its database mutex serializes
access but does not guarantee the order in which separate IPC invocations
acquire it. An independent regression run against the original implementation
failed: an older delayed write persisted `all` after the UI selected `completed`.
The same race passes with CC's per-store Promise queue. The queue continues
after rejection and suppresses errors from obsolete filter revisions. Six
focused store tests and five mounted search tests passed in CC's run; a separate
Node 22/pnpm 10 run passed 24 retained content/task/workspace tests.

The feature adds a localized clear button and Escape handling to Notes search,
retaining focus, filters and the active draft. The reviewer required actual
Escape events for both IME guards, observable parent bubbling for empty searches,
and a controlled test harness that really updates the input and calls its spy.
Early test drafts lacked those checks; their intermediate results are not used
as acceptance evidence.

Review also caught a browser-specific CSS defect in CC's initial implementation:
grouping `::-webkit-search-cancel-button` and `::-ms-clear` in one selector list
caused Brave to discard the entire rule. An isolated real-CDP page returned zero
CSSOM rules for that list, and retained the WebKit rule when they were split.
CC applied the split. The new `TC-NOTES-014` case also initially combined the
default status with Pinned while expecting Pinned alone, and its verification
step references were misnumbered. Those were returned for correction before
hosted execution; a reviewed plan is not an executed pass.

One independent command accidentally used the system pnpm version, triggering a
dependency reinstall and a subsequent `vitest not found` setup failure in CC.
The reinstall was stopped and the frozen dependency tree restored offline with
Node 22/pnpm 10 in 3.7 seconds. The lockfile was unchanged. That setup failure is
kept separately from the actual pre-fix race and post-fix test results.

Product commit, final independent review and hosted execution receipts remain
pending at this point in the investigation.

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

All 138 offline tests passed before the subsequent monitor correction; the full
offline discovery then passed all 139 tests, including that regression. The
real-CDP fixture covers both specific pool and
generic server failures through both API protocols: four cases, two fixture
requests each, all successful. Network failures after delivered content still
end explicitly; the underlying provider/network cause remains unestablished.

At 00:20:15 on November 10, the canary service on port 8084 was gracefully
restarted at an idle API boundary and loaded `2c2674dc6e2e8ae294eb250a1b92e0f0a76222c1`.
The supervised CC client then continued issuing successful requests. Its single
`kcanary` tab is independent of the original five `kpool` tabs.

The original background-agent transcripts record interruption at 23:53:46.
The original main session continued receiving data. One later max-effort request
spent 713 seconds producing reasoning before returning three Read calls with
`tool_calls` as its finish reason. The following prompt contained 192,558 tokens.
These observations distinguish slow model work from a transport deadlock. The
main session completed its full architecture report at 00:28:59 on November 10.
It explicitly distinguished static findings and historical QA records from
tests executed in that session. Port 8083 was then observed idle with all five
tabs ready; its service replacement is the next operational step.
