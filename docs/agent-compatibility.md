# Agent tool history and long requests

Some NVIDIA Playground tool responses reuse IDs such as `Read:0` in successive
turns. Claude Code 2.1.319 can remove later calls and results with those IDs
before its next request, replacing a message with `(no content)`. Remapping
IDs only when receiving that next request cannot restore the missing history.

The shared output event layer now assigns a fresh `toolu_…` ID to each tool
invocation. The ID remains stable throughout that invocation's argument
fragments. This applies to Messages and Responses, both SSE and JSON. Names,
arguments, text and thinking are unchanged. Clients must return the ID they
received in their tool result. Historical repeated IDs in Messages requests
remain supported by the existing input mapping.

`HISTORY_REASONING=omit` excludes previous assistant `reasoning_content` at
the common upstream boundary for both APIs and both response formats. It
preserves visible messages, tool names/arguments/results, effort and budget,
and still streams newly generated reasoning to the client. The original
request and stored Responses items are not modified. `preserve` retains the
previous behavior and remains the library default; `/health` reports the
selected policy. Set the environment variable before starting the service.

A local mocked-response check of the native Playground UI retained its final
answer in the next user request but omitted its old reasoning. A held-out
Claude comparison completed with 24/24 external checks when old reasoning was
omitted; a preserving run also saved 24/24 code but required interruption
after repeated deliberation. This supports testing the option for Kimi,
without establishing a universal quality improvement across workloads.

Kimi's official K3.1 Pro API documentation requires preserving the complete
assistant message, including `reasoning_content`, in both multi-turn chat and
tool loops. The NVIDIA deployment and its web UI may behave differently;
the local UI observation does not override that API contract. Keep the
default `preserve` policy for general use. `omit` is an experimental option
for this NVIDIA deployment, with one successful repair-task comparison; it
has not been validated across the three-stage development benchmark.
See [Kimi thinking models](https://platform.kimi.com/docs/guide/use-thinking-models)
and [K3.1 Pro quickstart](https://platform.kimi.com/docs/guide/kimi-k3-1-pro-quickstart).

This cannot reconstruct tool history already discarded by a client. Save the
current files and unfinished requirements, then use a fresh session when an
old Claude conversation has already lost calls or results.

`LOOP_BREAKER` now defaults to `0`. The previous process-wide counter treated
replayed histories as new executions, associated independent conversations by
their first 200 user characters, and could instruct an agent to summarize
before completing a new development stage. With `LOOP_BREAKER=1`, the bridge
now checks only the supplied history since the latest user message. Three
consecutive identical calls must each have a matching identical result before
it adds a reminder to inspect the results and continue the task. It never asks
for early completion or claims the tools succeeded. Results beginning with
`Error:` or `<tool_use_error>` are excluded. Tool error encodings vary, so this
heuristic remains optional; no session counters are retained after compaction.

`BROWSER_FIRST_BYTE_TIMEOUT` now defaults to `BROWSER_TIMEOUT` (3600 seconds).
The previous 120-second default could abort a valid slow reasoning request.
The total request deadline, explicit first-data override, idle timeout,
downstream heartbeat and disconnect cancellation still apply.

An optional integer `seed` extension now passes through both APIs, including
`0`, which was previously dropped. Absent/null leaves the provider default
unchanged, and invalid types receive HTTP 400. This is a bridge extension and
does not imply that the provider guarantees deterministic generation.

Payload diagnostics now count system characters in `messages`, where the
upstream prompt actually resides, rather than the unused top-level `system`.

Messages JSON aggregation now rejects invalid or non-object tool parameters
with HTTP 502. Previously, invalid JSON became `{}` and was reported as a
successful tool call, obscuring the provider failure. The bridge does not
execute malformed tools or retry an already delivered stream to replace them.

`GET /health` exposes `tool_ids: "unique_per_invocation"` and
`loop_reminder_enabled` so a restarted bridge can be distinguished from an old
process. These compatibility fixes do not promise identical model answers.

CC can replace `CLAUDE_CODE_EXTRA_BODY.max_tokens=131072` with `64000` when
falling back from an interrupted stream to a non-streaming request. Both
2.1.110 and 2.1.321 exhibit this in a controlled local CDP fixture. For a
benchmark that requires an identical output ceiling on every attempt, set
`BROWSER_FIXED_MAX_TOKENS=131072` explicitly on the bridge. The default is
`0`, which preserves smaller client limits. The setting applies to Messages
and Responses in browser mode, must not exceed `BROWSER_MAX_TOKENS`, and is
exposed as `browser_output_tokens` in `/health`. Overrides are logged as
`browser_output_limit_override` with the requested and effective values.
The same endpoint reports the loaded total, first-byte and idle deadlines in
`browser_timeouts`; increasing the total alone does not increase the default
90-second idle limit.

The official Node.js CC CLI has another fallback constraint: Node fetch can
close a non-streaming request while waiting for headers after about 301
seconds even when CC's `API_TIMEOUT_MS` is larger. A local real-CC probe
captured `UND_ERR_HEADERS_TIMEOUT`; the native CC distribution waited for
the delayed 310-second response. Non-streaming bridge responses intentionally
retain the final HTTP error status and do not send success headers early.

For the Node CLI, an explicit optional adapter aligns fetch's header/body
timeouts with the process's `API_TIMEOUT_MS` for the configured
`ANTHROPIC_BASE_URL` origin. Install its locked dependency with
`npm ci --ignore-scripts --prefix tools/cc_node_runtime`, then start the
unmodified official `cli.js` with
`node --require /absolute/path/tools/cc_node_runtime/timeout.cjs /path/to/cli.js ...`.
Both variables must exist in the process environment before Node starts;
putting them only in CC settings is too late for a preload. Other origins and
AbortSignal cancellation retain their original behavior. This is an opt-in
Node runtime workaround, not a change to CC source or a fix for upstream
HTTP/2 faults. A client-supplied dispatcher can bypass the global adapter;
validate the actual client/proxy configuration when using it outside the
isolated evaluation setup.

`node --test tests/test_cc_node_timeout.cjs` checks slow response headers,
slow bodies, cancellation, and origin scoping. The opt-in
`tests/probe_cc_http_timeout.py` uses a real CC process and local Messages
fixture with a 310-second delayed fallback response. Supply `--output` and
`--pinned-cli`, optionally `--adapter`, `--adapter-only`, or `--include-native`.
It records passive Undici errors without capturing request credentials or
contents. These probes do not invoke NVIDIA or evaluate model quality.

The opt-in `tests/probe_cc_midresponse.py` runs real CC processes against an
isolated local HTTP/CDP fixture. It truncates an HTTP 200 stream after text,
thinking, thinking plus text, or complete tool JSON without a finish marker,
then checks client recovery, explicit session resume when needed, and a
single counter write. It never sends NVIDIA requests or opens benchmark
inputs. Supply `--output`, optionally `--pinned-cli` for an official Node.js
`cli.js`, and `--fixed-max-tokens 131072` to verify the override. The browser
must already be running on the selected `--cdp-port`; only the probe's own
tab is created and closed. This tests protocol behavior, not model quality
or the cause of a real provider's network failure.

Offline regression coverage includes all four protocol/format combinations,
interleaved calls, late metadata, Unicode argument preservation, stored
Responses continuations, early-close propagation, independent sessions,
resent histories, failed tools, changing results and new user stages.
