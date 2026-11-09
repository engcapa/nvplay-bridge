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

Offline regression coverage includes all four protocol/format combinations,
interleaved calls, late metadata, Unicode argument preservation, stored
Responses continuations, early-close propagation, independent sessions,
resent histories, failed tools, changing results and new user stages.
