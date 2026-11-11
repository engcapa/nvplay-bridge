# 上游故障与恢复观测：2026-11-11 至 2026-11-12

记录截至 2026-11-12 02:50，北京时间。对象为开发会话使用的 8083 bridge、
Brave/CDP 9225 及 NVIDIA Playground 的 Kimi K3.1 Pro 路由。

## 结论与范围

故障期间的请求实际发送后，NVIDIA 上游返回 HTTP 500，正文为
`Retries exhausted: 3/3`。独立的单消息、`low` effort、512-token 上限探测也收到
相同错误，因此故障不限于 CC 长上下文或 `max` effort。

没有发现连接池排队、转发吞吐或请求清理成为当前瓶颈。bridge 保持健康，
按现有预算退避重试并释放失败请求占用的标签页。

01:30 起，短请求恢复成功；截至 02:45，7 次短请求检查均一次成功。
这只证明观测时该账户、该路由的短请求可用。CC 实际长上下文任务尚未在恢复后重试，
不能据此认定长任务已恢复，也不能确定服务商的具体故障原因或精确恢复时刻。

## 参数与请求链

真实请求的关联日志确认：

| 参数 | CC 请求 | bridge 发往上游 |
|---|---|---|
| effort | `output_config.effort="xhigh"` | `reasoning_effort="max"` |
| 正常流式输出上限 | `max_tokens=128000` | `max_tokens=128000` |
| CC 非流式回退输出上限 | `max_tokens=64000` | `max_tokens=64000` |
| 非流式回退传输 | `stream=false` | 上游仍流式接收，bridge 汇总后交付 |

`_upstream_effort` 将 `xhigh` 归一为 `max`。此次故障处理中未更改 effort、
输出预算、历史推理策略或重试配置。开发会话显式 effort 并非 `low`；
`low` 仅用于独立诊断。

## 故障证据

- 故障前最后一次真实开发请求于 23:35:40 成功，报告输入 379802、
  输出 8149 token，其中推理 8148 token。
- 23:35:41，开发请求 `6fb84c55d7bf488da8e59640` 开始。
  23:37:52 收到首个上游 500。4 次尝试后约 544.867 秒结束，
  CC 随即进入非流式回退。
- 至最后一个已确认失败响应 01:19:05，共记录 40 次上游 HTTP 500、3 次 HTTP 400，
  期间没有成功请求。40 次 500 均带完整错误正文；发送后等待时间中位数 124.667 秒，
  范围 122.581–131.543 秒。
- 500 正文来自上游路由 `/v2/predict/models/qc69jvmznzxy/kimi-k3_1-pro`，
  `detail` 和 `cause` 均为 `Retries exhausted: 3/3`。
  这是上游正文；bridge 另外执行配置的 3 次额外重试，最多 4 次尝试。
- 00:05 的 3 次 400 正文指向上游内部 `grpc.nvcf.nvidia.com/v1/chat/completions`，
  未给出具体参数校验原因。不能仅凭这一通用正文确定是上下文长度、effort 或其它参数问题。
- 一条极短 `xhigh → max`、64000 上限探测的 4 次尝试全部收到相同 500，
  总耗时 533.256 秒。另有两条 45 秒硬上限探测在没有上游响应时取消，
  它们不能用于推断最终 HTTP 状态。
- 00:48:05 发出的独立单消息 `low`、512-token 探测，在发送后 122.828 秒收到相同 500。
  旁路日志确认只产生一次上游尝试；在重试退避期间主动取消，清理完成。
- CC 多次自动重发非流式请求。01:20:40 最终记录 `Request timed out`，
  之后截至本记录时未再发送实际开发请求。bridge 回收完毕，5 个标签页全部就绪。

流式接口提前返回的 HTTP 200 只代表 SSE 连接已建立；需要检查上游状态及
`message_stop`/完整输出，不能把心跳或 `message_start` 计为推理成功。
监控中的 `browser_error`、`messages_failed` 包含透传的上游错误，
不应仅按事件名将它们归因为 bridge 内部缺陷。

## 恢复检查

下列请求均为单消息、要求只回复 `OK`，结果完整，只有一次上游尝试。
耗时为诊断客户端端到端计时，包含 bridge 准备与交付。

| 发起时间 | 请求 effort → 上游 effort | 输出上限 | 总耗时（秒） | 结果 |
|---|---|---:|---:|---|
| 01:30:24 | low → low | 512 | 5.512 | OK |
| 01:32:35 | xhigh → max | 64000 | 4.515 | OK |
| 01:45:20 | low → low | 512 | 14.899 | OK |
| 01:59:49 | low → low | 512 | 5.223 | OK |
| 02:15:01 | low → low | 512 | 6.802 | OK |
| 02:30:03 | low → low | 512 | 5.581 | OK |
| 02:45:06 | low → low | 512 | 6.576 | OK |

首个成功探测于 01:30:29 完成，前一个已确认失败响应为 01:19:05；
期间没有足够连续样本确定恢复发生在哪一刻。

## bridge 与浏览器资源

bridge 为 22:17:59 启动的实例 `242915ff9f80`、PID 825457，
本轮上游故障期间没有重启或更改生产源码。
常态仅一个 CC 请求占用标签页；诊断并行时为两个，其余就绪。
错误或客户端取消后正常回收，未观察到标签页泄漏。

故障首小时 61 次监督采样的 bridge 健康状态均为 `ok`；RSS 约 65–73 MiB，
文件描述符 12–14 个。恢复后空闲时约 73 MiB、12 个描述符、2 个线程，持续稳定。
02:22 对 5 个页面只读调用 `Runtime.getHeapUsage`，已用 JavaScript 堆约
25–45 MiB，宿主机约有 25 GiB 可用内存。上述快照没有显示当时存在内存压力，
单次浏览器快照不足以排除所有长期泄漏。

## 变更与本地证据

此前 bridge 修复已在 `c0b8d1c1b34f673c997f7e18c7b00ae4b0d8fc3b` 推送，
验证为 286 项 unittest 和 4 项隔离真实 CDP 探针通过。
本次新增内容为审查记录及 git 忽略目录中的诊断脚本、证据，没有新增生产代码修复。

以下本地日志不提交至 Git；其中请求 ID 可与 bridge 元数据交叉核对：

- `logs/upstream-500-incident-live-2026-11-11.json`：更新中的请求与错误时间线。
- `logs/upstream-one-hour-incident-2026-11-12.json`：故障首小时采样汇总。
- `logs/upstream-connectivity-probe-2026-11-11-2346.json`：最初极短请求的四次 500。
- `logs/upstream-single-attempt-low-probe-2026-11-12.json`：单次 low/512 失败证据。
- `logs/upstream-single-attempt-*-probe-*.json`：各次有硬时限的恢复检查。
- `logs/upstream-recovery-evidence-2026-11-12.json`：首批 low 与 xhigh 恢复检查的关联证据。
- `logs/browser-resource-snapshot-2026-11-12-0220.json`、
  `logs/browser-heap-snapshot-2026-11-12-0220.json`：浏览器与宿主机资源快照。
- `logs/supervised-watch-2026-11-11.jsonl`：监督采样、参数和桥接资源记录。

后台采样与监督检查继续运行；实际长任务恢复仍需后续真实请求验证。
