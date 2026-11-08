# OpenAI Responses compatibility

## Interface and sources

The bridge adds HTTP `POST /v1/responses` alongside the existing Anthropic
Messages API. A request with `stream=true` receives SSE; otherwise it receives
a Responses JSON object. Both paths use the same upstream adapter, pool,
timeouts, heartbeats, preparation recovery and disconnect cancellation.
`GET /v1/models` includes OpenAI model-list fields while retaining its existing
Anthropic fields.

Implementation references, fetched on 2026-11-08:

- [Create a response](https://developers.openai.com/api/reference/resources/responses/methods/create)
- [Responses streaming events](https://developers.openai.com/api/reference/resources/responses/streaming-events)
- [Streaming responses](https://developers.openai.com/api/docs/guides/streaming-responses)
- [Codex advanced configuration](https://developers.openai.com/codex/config-advanced)
- [Codex web search defaults](https://learn.chatgpt.com/docs/web-search)

The installed Codex 0.167.0 request was also captured using a local fixture
server and an isolated home. It sends `stream=true`, `store=false`, reasoning
effort/summary options, function tools, namespace tools, developer messages
and explicit history. It accepts the Responses event and usage shapes without
requiring an OpenAI login when the custom provider disables OpenAI auth.

## Supported inputs

| Input | Mapping |
|---|---|
| `input` string | One user message |
| Message items and easy messages | User/assistant/system messages; developer maps to upstream system |
| `input_text`, `output_text`, refusals | Upstream text content |
| `input_image` URL or data URL | Upstream image content; actual vision depends on the configured model |
| `instructions` | Per-request system instructions; not inherited through previous response IDs |
| Plain reasoning summaries/content | Historical upstream `reasoning_content` |
| Function calls and outputs | Preserve `call_id` across upstream assistant/tool messages |
| Custom calls and outputs | Preserve raw input through a JSON wrapper |
| Function/custom namespaces | Flatten to collision-checked upstream names and restore `namespace` on output |
| `reasoning.effort` | low/medium/high/max; xhigh maps to max; minimal/none map to low |
| `max_output_tokens` | Upstream output budget, capped at the configured upstream limit |
| `tool_choice`, `parallel_tool_calls` | Auto/none/required/named function/custom choice and parallel hints |
| `text.format` | Text, JSON object or JSON schema passed to the configured upstream |

The backend has no switch that guarantees zero reasoning. `none` and `minimal`
therefore select the lowest supported effort rather than claiming reasoning
is disabled. Existing Kimi reasoning is exposed as Responses summary text;
the bridge does not create OpenAI encrypted reasoning or signatures.

Custom tools are represented upstream as functions taking one `input` string.
Text and grammar definitions are included in their descriptions. Raw input
is decoded incrementally, including escaped newlines and Unicode surrogate
pairs. Grammar enforcement remains the model/client's responsibility; the
bridge validates the JSON wrapper, not arbitrary Lark or regex grammars.

Sampling and verbosity hints are not translated for the fixed-sampling Kimi
Playground. Cache keys, client metadata, include hints and service-tier hints
may be accepted without implementing their OpenAI server-side behavior.
Encrypted historical reasoning cannot be decrypted; send text history/tool
results for this upstream to continue a conversation.

## Output and streaming

Events have an increasing `sequence_number` and stable output item IDs:

- `response.created`, `response.in_progress`
- `response.output_item.added/done`
- `response.content_part.added/done`, `response.output_text.delta/done`
- `response.reasoning_summary_part.added/done`, `response.reasoning_summary_text.delta/done`
- `response.function_call_arguments.delta/done`
- `response.custom_tool_call_input.delta/done`
- `response.completed`, `response.incomplete`, `response.failed`

Multiple tool items can stream independently using their own output indexes.
Tool inputs are checked before successful completion. Responses may contain
reasoning, messages and tool calls together. Text and reasoning deltas are
forwarded immediately; their final full values appear in done items and the
terminal response. Usage uses the final upstream cumulative counters,
including cached input and reasoning output tokens.

Idle connections receive SSE comments (`: ping`) without adding invented
Responses event types. The browser's existing idle and total timeouts still
apply. On length/content-filter termination the response is incomplete with
`incomplete_details`. A broken stream, missing finish reason, malformed tool
input or upstream error fails instead of emitting a successful completion.
Partial responses are never silently replayed. Invalid requests return an
OpenAI-style HTTP error body before generation begins. Client disconnects
cancel both streaming and non-streaming generation.

Before any thinking, text or tool content is delivered, the browser adapter
can retry the specific NVIDIA HTTP 500 pool-acquire timeout at most twice.
It keeps the same response ID and SSE connection, sends heartbeat comments
during backoff, and shares the request's original total deadline. Other
HTTP/network failures are not retried by this policy. An exhausted retry
emits one `response.failed` containing the original error; it never emits
`response.completed`. See [retry policy](upstream-pool-timeout-retry.md).

## Stored history

`store` defaults to true. Stored completed/incomplete responses support
retrieval, deletion and `previous_response_id` continuation. New requests
replace instructions and tool definitions; historical calls retain their
identity and namespace mapping. `store=false` creates no retrievable record.

Storage is process-local, limited to 128 records and 32 MiB, with a one-hour
TTL and least-recently-used eviction. Restarting the service removes stored
IDs. Requests and response output have 16 MiB bounds; each tool input has a
1 MiB bound. Storage overflow fails explicitly instead of claiming an ID is
retrievable. This is a local compatibility feature, not durable OpenAI storage.

## Explicit boundaries

This interface does not implement WebSocket transport, background responses,
hosted web/file search, computer-use tools, server-side shell execution,
audio/video/files, conversations, hosted prompts or server compaction. Send
explicit input history and use client-executed function/custom tools. Unsupported
input/tool types return a validation error rather than being silently dropped.
Input images are translated but vision capability has not been validated.

Codex includes an optional nameless `web_search` declaration by default, even
for a greeting. Top-level `web_search` and the known dated/preview variants
are accepted as optional capability advertisements so they do not block text
or client tools. They are omitted from upstream tools and the response's
effective `tools` list. The model receives an explicit note that built-in
search is unavailable, and diagnostics record `responses_capability_unavailable`.
An explicit named search choice, or `required` with no available client tools,
returns an error. This does not provide hosted search results or citations.
Other unsupported tools still fail with their `.type` parameter; malformed
function/custom tools still fail with their `.name` parameter. Validation
diagnostics record tool types and name presence without prompts or arguments.

The Codex example explicitly disables WebSocket transport and hosted web search.
The live CLI regression also tests the default search advertisement without
setting `web_search="disabled"`. Its
large context threshold avoids using a server compaction route. Unknown model
aliases can cause Codex to use fallback model metadata; this is reported by
Codex and does not rename Kimi as an OpenAI model.

## Verification

`tests/test_responses.py` covers request conversion and validation, sequence
numbers, reasoning/text lifecycle, parallel namespaced tools, custom input at
every split boundary, usage, truncation, errors, memory history, a real local
HTTP tool loop and cancellation. When installed, the official OpenAI Python
SDK is used to parse both JSON and its streaming accumulator.

`tests/live_responses.py --codex` uses separate browser tabs and a temporary
server to test real Playground inference, stored history, namespaced tool
results, custom input, disconnect recovery, concurrency and the local Codex
CLI. Codex receives a temporary home, no copied credentials, an isolated
workspace and `--ephemeral`. Only that temporary workspace is marked trusted
in the test config, using the documented `:workspace` permission profile.
On Windows the test selects the restricted MXC sandbox in that temporary
configuration and verifies it can start a command before calling the model.
This requires a supported Windows/Codex installation; it does not provision
the elevated sandbox or change the user's saved sandbox selection.
It must read an input file, compute and write a
result, read it back through actual shell tool calls, and return the verified
answer. Existing configuration/auth file hashes are compared before and after.

Execution results are recorded in `logs/responses-e2e-report.json`. The
[validation report](responses-regression-report.md) records this run's
coverage, observed timings, Codex file verification and compatibility limits.
