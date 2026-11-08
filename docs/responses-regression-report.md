# Responses API regression report

Date: 2026-11-08. Tested on Windows with Python 3.12, Brave/CDP on port 9225,
the actual NVIDIA Playground Kimi K3.1 Pro backend, OpenAI Python SDK 2.16.0,
and local Codex 0.167.0.

## Automated regression

The offline suite covers the existing Anthropic translation, framing,
streaming, cancellation, pool ownership and recovery behavior together with
the new Responses protocol. Tests use a real Uvicorn HTTP server where
transport behavior matters. The official OpenAI Python SDK parses both JSON
responses and its streaming accumulator, and lists the advertised models.

Responses-specific coverage includes:

- Developer/system instructions, images in the upstream request, explicit
  message/reasoning/tool history, effort and output budget mapping.
- Function and custom tools, namespaces, tool choice and parallel options.
- Text/JSON schema request formats, unsupported inputs and OpenAI error bodies.
- Ordered event sequences, stable item/call IDs, text and reasoning lifecycle,
  independent interleaved tool arguments, and final usage counters.
- Incremental custom input decoding at every split boundary, including quotes,
  newlines, backslashes, Chinese text and Unicode surrogate pairs.
- Delivery of text, reasoning and tool arguments while the upstream remains
  blocked before completion; heartbeat comments during an upstream pause.
- Length truncation, broken/malformed tool output and partial failures without
  a false completion or silent replay.
- Streaming and non-streaming HTTP disconnect cancellation.
- Stored history continuation/retrieval/deletion, store=false, TTL/LRU expiry,
  copy isolation, memory limits and replacement accounting. A storage failure
  cannot report a completed or retrievable response.

One existing readiness timeout test depended on a real 20 ms wait. A scheduler
wakeup could cause an extra poll and exhaust its fake page states. The test
now advances a controlled clock, preserving its readiness diagnostic assertion
without depending on Windows timer scheduling. Production recovery code is
unchanged by this test correction.

## Real backend end-to-end results

The opt-in runner starts a temporary HTTP server and three independently
named `responsee2e` browser tabs. It closes those resources afterward and
never leases or closes the production `kpool` tabs.

| Scenario | Observed result |
|---|---|
| Reasoning and text SSE | Correct 137 × 293 = 40141, marker RESPONSES_OK and real usage; first delta 44.703 s, completion 46.562 s |
| Non-streaming and stored history | Recalled blue-orchid-731 through previous_response_id; retrieval and deletion verified |
| Namespaced function tool | utilities.lookup arguments and call ID preserved; tool result LOOKUP_RESULT_882 reached the answer |
| Custom input streaming | Delta concatenation exactly matched raw Chinese/emoji/newline input |
| Client cancellation | Disconnected after a delta; upstream aborted, resources released and all test tabs recovered |
| Three concurrent requests | Each request received its own CONCURRENT_0/1/2 marker without mixing |
| Custom output continuation | A custom_tool_call_output was accepted and CUSTOM_RESULT_481 reached the answer |
| Parallel function calls | Two independent lookup calls in one response; distinct call IDs and both result values preserved |
| JSON schema output | Parsed output was exactly {"sum":42,"marker":"SCHEMA_OK"} |
| Installed Codex CLI | Actual shell read/write/readback, verified result.txt = 42 and final CODEX_RESPONSES_OK 42; 50.953 s |

These ten scenarios passed. Timings include browser preparation and upstream
generation, and do not measure the bridge's forwarding latency alone.
Deterministic gated HTTP tests separately prove delivery before completion.

## Local Codex isolation and verification

The test gives only the child process a temporary CODEX_HOME and workspace
under ignored logs/. It copies no authentication material, disables hosted
web search and WebSocket transport for this provider, and uses --ephemeral.
Only the temporary project is marked trusted, with the :workspace permission
profile and the restricted Windows MXC sandbox. A local sandbox startup probe
passes before any inference request. No administrator setup is performed.

Codex performs two successful shell commands: it reads numbers.txt containing
17 and 25 and writes their sum, then independently reads result.txt and
verifies its format and value. The harness checks the actual file, successful
tool completion events and final answer. It does not accept a model's claim
that a file was written as sufficient evidence.

SHA-256/existence checks on the existing user's config.toml and auth.json
match before and after. The active user's configuration and credentials are
neither replaced nor copied. The CLI test uses a separate provider URL and
does not attach to an existing Codex session.

## Deployment validation

| Check | Result |
|---|---|
| Offline Python regression | 88/88 passed: 65 existing tests and 23 Responses tests |
| Browser JavaScript harness | Unicode fragments, incremental draining, request isolation, abort and buffer bounds passed |
| Static checks | Python compilation, PowerShell/Bash syntax and Git whitespace checks passed |
| Responses on production port 8083 | Official SDK stream returned RESPONSES_READY; first delta 3.000 s, completion 4.578 s |
| Anthropic on production port 8083 | Stream returned BRIDGE_READY with message_stop and no error; first delta 3.438 s, completion 5.063 s |
| Codex against production port 8083 | Two successful shell commands, actual result.txt = 42, final CODEX_RESPONSES_OK 42 in 51.875 s; existing config/auth unchanged |
| Browser pool | Five production tabs initialized, no recovery errors; zero remaining responsee2e tabs |
| Monitoring | Background JSONL monitor and 15-second health probes running; no extra model traffic generated by monitoring |

The old launcher exited immediately on its stop signal and also stopped its
browser process, interrupting an active Anthropic connection. The new launcher
restored Brave and the five-tab pool. An Anthropic client subsequently
reconnected and received upstream data, and the independent deployed-interface
checks above passed. Future restarts must account for the launcher's process
group behavior when uninterrupted active connections matter.

Production uses the existing 3600-second total request budget. Runtime session,
launcher, monitor and status locations are recorded in
logs/active-test-session.json. Deployment evidence is saved in
logs/responses-deployment-smoke.json, logs/restart-smoke.json and
logs/responses-deployment-codex-report.json.

## Limits of this result

This validates the documented local Responses subset, not every OpenAI hosted
feature. WebSockets, hosted search, server-side compaction, background jobs,
audio/files and OpenAI encrypted reasoning are unsupported. Custom grammar
descriptions are forwarded, but arbitrary grammar enforcement is not provided.
Image request conversion is tested; actual model vision is not validated.
JSON schema is passed upstream; the observed valid result is not a guarantee
that this bridge independently enforces every schema constraint.

Stored response IDs are process-local with one-hour TTL, 128-entry and 32 MiB
bounds. They expire and disappear on restart. Codex uses store=false and
explicit history. Unknown Kimi aliases cause a Codex fallback-metadata warning;
the bridge keeps the actual Kimi identity. Browser idle/total timeouts remain
in effect; a heartbeat does not remove a client's own deadline.

## Reproduction

Configure PYTHONPATH for the repository and optional vendor directory, then
run the offline files listed in README and:

```powershell
python tests/live_responses.py --codex
```

Brave/CDP and Playground access are required. Windows CLI tool execution also
requires MXC support. --codex-only reruns just the isolated local CLI stage;
--extended-only reruns custom outputs, parallel tools and structured output.
Those targeted modes retain successful results already present in the JSON
report, so an earlier real inference need not be repeated unnecessarily.

Raw evidence stays in ignored logs/responses-e2e-report.json,
logs/responses-e2e-debug.jsonl and logs/codex-responses-e2e.*. Production
health and request metadata are monitored separately, without generating
additional model traffic.
