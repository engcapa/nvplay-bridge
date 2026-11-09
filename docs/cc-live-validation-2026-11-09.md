# Claude Code live investigation, 2026-11-09–10

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

The implementation was committed as
`591ce56ff55fa24794c392aaa18047c4e6766997` on that new branch and subsequently
pushed by CC. Its 13 changed files include the product changes, focused tests,
case, policy/dependency registrations, translations and generated control catalog.
The final case bytes match the independently executed copy (SHA-256
`f2ba370b547317b568b3fc5574aeb589d1a6410084db43202a7384fd7f62ff4f`).

CC's final focused run passed all 11 store/component tests. TypeScript,
`check:refactor`, the regenerated catalog, QA audit and the post-commit development
case contract passed. The reviewer independently checked the contract and the
24 retained Notes tests. An initial isolated-Python catalog command failed
because `-I` ignores `PYTHONPATH`; that setup failure was corrected before the
final successful audit.

An actual independent Linux browser run passed all 61 steps of `TC-NOTES-014`
and all three verification requirements. It used the existing Brave 157
executable through a local wrapper, recorded that executable's identity, and
did not change the repository runner. The receipt and final screenshot show an
empty focused search, the exact pinned/tag-filtered note and the retained dirty
draft. The explicit executable choice is supplementary browser evidence, not
evidence for hosted Chromium or native WebViews. Synthetic IME guards do not
certify physical OS composition or native menu/key delivery.

## Further live evidence and HTTP recovery

A real CC smoke session through the patched service called Bash and Read and
returned the independently checked SHA-256 and README title. It completed three
turns in approximately 133 seconds. The service on port 8084 used a separate
`kcanary` pool namespace while the original service and its active sessions
remained on port 8083 with `kpool` tabs.

A deliberate interrupt of the supervised implementation session at 23:27:11
produced a confirmed browser abort and `browser_cancelled` within approximately
55 milliseconds. The same CC session then resumed through the patched service.
Repeated analysis of a small test led to a later, fresh focused session with
medium effort and a smaller context. That session inherited the implementation
branch and test changes with exclusive ownership of the product files.

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
`kcanary` tab was independent of the original five `kpool` tabs.

The original background-agent transcripts record interruption at 23:53:46.
The original main session continued receiving data. One later max-effort request
spent 713 seconds producing reasoning before returning three Read calls with
`tool_calls` as its finish reason. The following prompt contained 192,558 tokens.
These observations distinguish slow model work from a transport deadlock. The
main session completed its full architecture report at 00:28:59 on November 10.
It explicitly distinguished static findings and historical QA records from
tests executed in that session.

A later independent spot review confirmed the report's concrete storage-startup
`expect` paths, release finalizer dependencies, retained terminal/SFTP mounting,
application version and dual tracked lockfiles. The original 713 recursive
case-file count is consistent with 714 after adding `TC-NOTES-014`. The startup
fault-isolation and retained-runtime observations remain static risks, not
reproduced database corruption or resource leaks. A release workflow's lack of
direct QA dependencies also does not establish the absence of external release
policies. The full source-line and IPC-command counts were not independently
recomputed. No broad product changes were made from these unproven risks.

## Production replacement and real recovery

At 00:35:37 on November 10, after observing the original service idle with all
five tabs ready, the service on port 8083 was gracefully replaced. PID 26861 was
replaced by PID 161859, loading bridge commit
`bac48c184e2ed5f75220a76666b7db66811284c3`. The original environment, uvicorn
arguments, CDP 9225 and five `kpool` tabs were preserved. The manual CC client
was held briefly at the idle boundary and resumed; no in-flight request was
terminated by the replacement.

A fresh CC smoke session through the replaced production service invoked Bash
once to hash `tools/monitor_bridge.py`, returned the independently checked digest
and `BRIDGE_MAIN_OK`, and completed two turns in 12.7 seconds with exit code 0.
The supervised implementation session also finished successfully with exit 0.

The first request of the subsequent CC workflow session, `c25fb817`, provided
real upstream retry evidence. Its first two attempts received HTTP 500 with
complete 330-character NVIDIA `Pool#acquire` error bodies after approximately
49 seconds each. The bridge classified both as pool-acquire timeouts, confirmed
the browser aborts, released the failed tabs and waited 5.712 and 10.290 seconds.
The third attempt used another healthy tab and completed at 142.795 seconds
with one tool invocation. No thinking, text or tool content had been delivered
by the failed attempts. The client session continued without an API error and
executed the requested Git operations. This is an actual NVIDIA recovery through
the production service, in addition to the controlled four-case CDP fixture.

At 01:06:11, a later request in that workflow session, `52bf8541`, again
encountered `TypeError: network error` while reading the upstream stream. It
had delivered 8117 thinking characters and 85 text characters over 81.071
seconds. The bridge logged `committed=true`, emitted an explicit error and
recovered its failed tab; it did not enter either HTTP retry policy. CC then
continued on the queued next instruction, and request `93aa1eb8` completed with
a tool call in 74.687 seconds. This recurrence shows the limit of the repair:
pre-content HTTP failures can recover inside the bridge, while already-delivered
network failures still require client continuation. The underlying reader/network
failure remains unestablished.

A supervisor settings copy initially retained an explicit
`CLAUDE_CODE_EXTRA_BODY` effort of `medium` despite its `low` CLI argument and
filename. Request logs confirmed that explicit body value was preserved; this
was a harness configuration mistake, not a bridge override. After correcting
only the ignored supervisor settings, a fresh short CC session completed a real
Bash branch/head check and final answer in two turns and 24.0 seconds. Both API
requests used actual `low` effort and returned the correct clean branch and
full reviewed commit. This ran alongside the older session's ongoing medium
request, using separate production pool tabs without mixed tool results. It is
concurrency/control evidence, not a matched effort-performance benchmark.

## Hosted execution

CC pushed `feat/cc-bridge-notes-search-20261109` and dispatched
[run 42659287606](https://github.com/engcapa/taomni/actions/runs/42659287606)
at 00:53:35 on November 10. The run's full head is the reviewed product commit
`591ce56ff55fa24794c392aaa18047c4e6766997`, not a historical result.

Inputs select `TC-NOTES-012`, `TC-NOTES-013`, `TC-NOTES-014` and the complete
`crash-regression` tag across Linux, Windows and macOS in browser/native modes,
with `native_release=true`, the default RDP unit contracts enabled and issue
publication disabled. The plan job passed. Independent comparison of the
downloaded `selection.json` against locally calculated expected sets found no
missing or extra cases in any of the six logical groups: 12 browser and 13
native per OS, 75 execution units in total. Each native group has four shards
sharing one release build. The selected native contract capability is `mobile`;
this case set does not select RDP contracts, even though their dispatch switch
remains enabled. By 02:07 on November 10, the actual workflow had completed
successfully, including all execution groups and final aggregation. The results
below are independently checked execution evidence.

The independent native contract job's execution passed: 129 related
frontend/mobile tests and 97 selected Rust tests under the release profile,
with zero failures. The Rust root test binary reported 1981 other tests filtered
out; this is capability-selected contract evidence, not a full Cargo test pass.

The successful Linux native producer also ran the full Vitest suite:
703 test files passed, with 6404 tests passed and one skipped. The skip belongs
to TerminalPanel's existing mutually exclusive text-mirror variants; neither
that case nor its condition changed here. The six workspace-store and five
search-component tests all passed. The native producer exported its release
runtime, subsequently used by all four Linux native execution shards.

All three hosted browser jobs have completed successfully. Independent raw
report checks found 12/12 cases passed per OS, with no skips. `TC-NOTES-014`
passed all 61 steps and requirements R1/R2/R3 on each. Source and runner
identities match the manifest; all case digests match; outcomes bind the exact
head, run, attempt and selection digest. Runner receipts verify and bind the
summary and artifact hashes, including Windows paths normalized only by the
reviewer's reader. All three final screenshots were inspected: the empty search
retains focus, the intended filtered note is selected and the dirty draft is
present. These final screenshots do not show the nonempty clear button and
therefore do not establish its decoration spacing across native WebViews.

All four Linux native shards subsequently completed and passed independent
raw-report checks: 13/13 cases, no skips. The new case passed 61/61 steps and
R1/R2/R3 in the actual packaged WebView. The app identifier is
`com.taomni.app.qa`; isolated data/config/cache paths are recorded. The release
binary hash is
`e87433534eca3f331dcc5351fab712964c36293d18d5052311a6295820343894`, identical
across all four shards and their build/summary identities. The Ubuntu 22.04
producer's runtime ran on the selected Ubuntu 24.04 Xvfb/Openbox desktop with
readiness and the expected desktop metadata verified. The final native
screenshot was independently inspected and agrees with the retained draft,
filtered note and search focus assertions.

All four macOS native shards also passed raw-report checks: 13/13 cases, no
skips, and 61/61 steps plus R1/R2/R3 for the new case. The ARM64 release build
hash is `04f46a4cc2ae26338791c7d1c13e2f8540adb8d451220b9c5a43b8b56749e5be`,
shared by all four shards and bound to the same reviewed source. The final
native screenshot was inspected. This is hosted native WKWebView/IPC evidence;
it does not certify physical OS IME, global shortcut or menu-bar input delivery.

All four Windows native shards passed the same raw-report checks: 13/13 cases,
no skips. The release binary hash is
`247ad81e303914e7a42c328afc71e1321047ccdf3efda46fe26db9303cb16dbe`, shared by
all four shards and matching their build/summary identities. The isolated QA app
ran with the Windows WebView2 adapter. The new case passed all 61 steps and
R1/R2/R3; its final screenshot was independently inspected and shows the focused
empty search, retained filters, selected note and unsaved draft.

| Hosted OS | Browser | Native release | TC-NOTES-014 in each mode |
| --- | ---: | ---: | --- |
| Linux | 12/12 | 13/13 | 61/61 steps; R1/R2/R3 passed |
| Windows | 12/12 | 13/13 | 61/61 steps; R1/R2/R3 passed |
| macOS | 12/12 | 13/13 | 61/61 steps; R1/R2/R3 passed |

The 15 raw execution entries contain 75/75 passing units, with zero failed or
skipped UI cases: 18 Notes units and all 57 applicable crash-regression units.
This is the selected Notes/crash set, not all 714 repository case files.
The independent reader verified every receipt-listed artifact's actual SHA-256
and required all selected case digests, outcomes and native release identities
to match. It found no missing entries or errors.

Recomputing `qa_ui_auto.ci.aggregate` from those 15 raw report directories
produced exactly the downloaded `ci-summary.json`, excluding only the latter's
additional `job_results` field. The downloaded final selection also equals the
original plan. Both `passed` and `report_ok` are true; `failures`,
`infrastructure_errors`, `gaps` and `unreviewed` are empty. All selected producers,
execution groups and mobile contracts report success. Tool/SFTP/development
contract jobs outside the dispatch selection were skipped; the development
contract had already passed independently against the product commit.

The canonical selection digest is
`882cebc91e09309d4d1f9fe7a5c3b2362bb3a50d3151f64ffb576dc54cdf307b`.
The source fingerprint is
`2deda7807ab6e064916be68f9760596e962c04c44e1bfe2876eafb3b12124420`,
and the runner fingerprint is
`50b8b5a550a9d7c082ab5d55b4651f5998e7828ffe3dbf8809876e380a0d128d`.
All six logical groups bind the same full reviewed head, run 42659287606 and
attempt 1. The canonical selection digest is intentionally distinct from a
raw selection-file byte hash.

The final CC session reran the reviewed raw-report checker, independently
extracted all four Windows build identities and queried the actual GitHub run.
Its final conclusion agrees with the independent checks: 75/75, no pending
entries/errors and `completed/success` at the exact reviewed head. One generated
extraction command incorrectly treated per-group `selected`/`counts` as top-level
aggregate fields and raised `KeyError`; corrected commands completed before
acceptance. Earlier ad hoc commands also confused a path argument and the
canonical versus byte-file digest. These were verification-script mistakes,
not passing evidence or bridge transport failures. The final review finished
at 02:12:33, and its supervisor-owned CC process exited normally with code 0
after stdin closed.

Receipt verification here establishes consistency with the repository's current
receipt format, not an unforgeable attestation boundary. Independent inspection
of `runner_receipt.py` found public default key records and a 32-bit FNV-derived
repeated checksum in `compute_receipt_signature`, despite its cryptographic
docstrings. Those checks alone cannot prove execution against a malicious report
author. This investigation also checks the actual GitHub job execution, exact
tested commit, original artifacts, SHA-256 bindings and case outcomes. Replacing
the product's receipt protocol was outside this Notes/bridge change.

## Bridge commits and remaining limits

The four bridge code/test commits were made directly on `main`, as requested:

| Commit | Change |
| --- | --- |
| `dba70af` | Preserve bounded delayed HTTP error bodies and count distinct tool calls. |
| `30987a2` | Isolate opt-in live probes and retain partial/replaced monitoring logs. |
| `2c2674d` | Retry confirmed pre-content HTTP server errors without replaying delivered content. |
| `bac48c1` | Keep old active requests visible outside the monitor's event window. |

All 139 offline tests and the four real-Brave/CDP fixture scenarios passed before
the final documentation update. No further product or bridge source changes
were made after their respective checks. The taomni branch is pushed; these
bridge commits remain local on `main`.

At the final 02:13:16 snapshot, the updated production process had handled 58
logical browser requests: 57 normal finishes and the one committed-stream
reader failure described above. Two internal HTTP retries produced 60 upstream
attempts in total; there were no cancellations, active requests or pool-recovery
errors in this post-replacement interval. This is a workload observation, not a
statistical reliability estimate. The service remained healthy with all five
production tabs ready.

Supervisor-owned implementation/workflow/final-review clients, the temporary
canary service and page, the local Vite server and the hosted monitor have
finished or been stopped. Production port 8083, Brave/CDP 9225 and the original
manual CC process remain running. Raw transcripts, settings, logs and downloaded
artifacts remain ignored local evidence and are excluded from the commits.

The remaining observed transport limitation is the midstream reader failure,
whose underlying cause is still unknown. Physical OS IME and global/menu input
delivery, nonempty clear-button decoration spacing, the receipt protocol's trust
boundary and unselected product capabilities remain outside the verified scope.
The six successful UI combinations do not establish those claims.
