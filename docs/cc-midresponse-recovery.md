# Claude Code 流中错误恢复

## 现场触发链

2026-11-11 18:17:28（Asia/Shanghai），请求 `d991013d135b4dea9fc3a4cf` 在生成约 309.6 秒后失败。上游 HTTP 状态为 200，SSE 内返回：

```json
{"error":{"message":"Service temporarily unavailable","type":"service_unavailable","code":503}}
```

此前桥接已经交付 18824 个 thinking 字符和 70 个正文字符，尚无工具调用。旧归一化逻辑只保留错误字符串，把它作为 Anthropic SSE `api_error` 发给客户端；既没有保留独立的结构化错误码，也不能撤回已经交付的正文。CC 会话随后记录 `server_error` 和 `Server error mid-response. The response above may be incomplete.`，结束当前轮次，未自动续发请求。

这个停止指 CC 当前执行轮次因响应中断而结束，不是模型正常生成完毕、输出 token 用尽、桥接进程退出，也不是 CC 进程退出。根始故障来自上游，桥接当时缺少安全的恢复边界。增大 `API_TIMEOUT_MS` 不能改变这条路径。

CC 是否自行重试还取决于运行方式和进度判断。在同版本原生 CLI 的隔离 `-p` 探针中，旧桥接也可能由 CC 另建请求恢复。因此不能声称每一个流内错误都会终止 CC；本次生产轮次停止有直接日志证据。新逻辑把符合条件的恢复移到桥内，避免依赖客户端的这些差异。

## 交付与重试

按用户选择，browser Messages 默认使用 `BROWSER_MESSAGES_DEFER_OUTPUT=1`：

1. 立即发送 `message_start`，实时转发开头的 thinking，按配置发送心跳。
2. 从首段正文或工具参数开始暂存后续输出；之后若再次出现 thinking，也随输出暂存以保留顺序。尚未交付正文、工具及 thinking 的结束事件。
3. 收到完整上游结束、完成标记，并通过内容顺序、工具元数据及 JSON 对象校验后，按原顺序交付暂存事件，发送真实的 `stop_reason` 和正常结束事件。
4. 明确的临时故障发生在这个交付边界前时，关闭失败流、释放页面，丢弃失败正文/工具，在同一客户端连接有限重试。错误重试保留请求体、max tokens、effort、历史、工具及采样参数；沿用原来的空回答纠正策略是单独的例外。
5. 达到次数或总时限后明确返回错误，不伪造 `end_turn`、工具成功或完成标记。持续的上游故障仍可能使客户端停止。

已显示的失败轮次 thinking 无法撤回，会和新轮次 thinking 一起保留在响应及后续历史中；最终 usage 对应成功轮次，各失败轮次 usage 单独留在诊断日志。正文与工具不再逐片实时显示，而在本次上游响应成功后集中交付。连接和思考仍为流式，生成耗时不会因暂存而缩短。

工具执行只发生在 CC 收到工具调用之后，桥接不会执行工具。失败轮次的工具即使已经形成完整 JSON，也不会交付；工具参数内容保持原样，成功交付时继续使用每次调用唯一的 ID。`length` 仍映射为 `max_tokens`，只有思考的 token 截断也保留这个事实；不完整工具 JSON 明确失败。

非流式 Messages 暂存全部内容，重试时丢弃失败轮次的 thinking。Responses 以及 `BROWSER_MESSAGES_DEFER_OUTPUT=0` 保持立即交付行为，已经交付的 thinking、正文或工具会阻止静默重放。

暂存超过 256 KiB 时转为临时文件，单轮上限 64 MiB；成功、失败、重试及取消都会关闭文件。正文持续生成但尚未交付时，桥接独立计时并发送心跳，避免上游持续有数据却让下游失去心跳。

## 退避和 Brave 页面恢复

| 错误来源 | 默认额外重试 | 等待时间（含抖动） |
| --- | ---: | --- |
| 明确 HTTP 500/502/503/504，包括 `Retries exhausted: 3/3` | 3 | 5–6、10–11、20–21 秒 |
| NVIDIA pool-acquire HTTP 500 专项 | 2 | 5–6、10–11 秒 |
| HTTP 200 内结构化 SSE 500/502/503/504，或明确读流故障/首数据超时/空闲超时/无完成标记的 EOF | 2 | 5–6、10–11 秒 |
| HTTP 响应前 fetch TypeError | 1 | 2–2.4 秒 |

公式为 `min(30, base * 2^retry) + uniform(0, min(1, backoff * 0.2))`。各策略独立计数，pool 专项优先，不借用通用 HTTP 次数。所有尝试、排队、准备和退避共用原请求的 `BROWSER_TIMEOUT`，客户端取消立即停止后续尝试。上游错误文案中的 `3/3` 属于上游自己的计数。

保留已有页面池恢复机制：失败请求先清理、归还后将页面隔离到 pending，由后台恢复线程刷新、激活、等待可用并重新准备；只有 ready 页面才能重新借出。重试不持有失败页，可以借另一健康页继续；只有一个页面时等待其恢复。不会为重试刷新其他忙碌页面或重启 Brave。页面刷新只能恢复页面状态，不能保证修复 NVIDIA 服务端故障。

现场已有证据：18:17:28 的失败页约 10.3 秒后重新 ready；18:33:27 的 HTTP 500 失败页约 10.9 秒后 ready，重试使用了另一页面。因此后续连续 HTTP 500 不能简单归因于未刷新页面。

`BROWSER_HTTP_5XX_RETRIES/BACKOFF` 和 `BROWSER_STREAM_RETRIES/BACKOFF` 控制新策略。`/health` 返回交付方式和实际退避参数。日志保留真实 HTTP 状态与独立的 `sse_error_code` / `stream_failure`；`browser_output_ready` 标记验证后的交付边界，`browser_validation_failed` 标记暂存期间的转换失败。

## 本地验证

离线回归覆盖同连接恢复、实时思考、暂存正文期间的心跳、工具参数保真、完整工具参数在流失败时不交付、真实 token 截断、不可重试分类、独立重试预算、截止时间、取消、临时文件释放和失败页后台恢复时切换健康页。

`tests/probe_cc_midresponse.py --fault sse-503 --require-automatic-recovery` 使用独立本地页面、Brave/CDP 和原生 CC 2.1.322，验证思考加正文、完整工具参数两种中途故障。默认 reader-error 模式另外覆盖这两种情形的读流中断。四种场景均在同一个 Messages request ID 内重试成功，无需恢复会话，失败工具执行 0 次，成功工具各执行 1 次。这些探针仅访问本地模拟服务，不发送 NVIDIA 请求，也不操作生产 CC 会话。

证据保存在 gitignored `logs/cc-midresponse-before-2026-11-11-1837/`、`logs/cc-midresponse-after-2026-11-11-1841/` 和 `logs/cc-midresponse-read-recovery-2026-11-11-1844/`。这些结果证明桥接恢复链路与客户端兼容性，不证明真实上游持续故障必然恢复。
