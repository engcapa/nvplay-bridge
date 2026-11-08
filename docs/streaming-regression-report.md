# Browser 流式转发回归记录

日期：2026-11-08。

## 结果

| 验证 | 结果 |
|---|---|
| 现有转换、重试、锁超时单测 | 21 项通过 |
| 新增流式测试 | 24 项通过；合计 45/45 |
| 页面补丁 JavaScript 验证 | 中文/emoji 分片、增量 drain、请求隔离、abort、缓冲上限通过 |
| PowerShell / Bash 启动脚本语法 | 通过 |
| Python 编译、Git whitespace 检查 | 通过 |
| 真实 Playground 思考/正文 SSE | 首内容 12.578s，整轮 16.703s，正文包含 STREAM_OK |
| 真实 Playground 工具参数 | 首内容 4.703s，整轮 4.922s，weather 参数为 Shanghai |
| 真实客户端取消 | fetch 终止；三个标签页均 done=true、pending=0，池槽全部归还 |
| 取消后恢复与三标签并发 | 中断标签页刷新后复用，三请求回复无串流 |
| Claude Code CLI | 首部分消息 11.344s，进程总耗时 12.282s，结果 CC_STREAM_OK |

首内容时间包含池初始化、页面发送和模型首 token 等待，不等于桥接转发耗时。真实 HTTP 测试在假上游仍阻塞于中途时已经收到 thinking_delta / input_json_delta，证明没有整轮缓冲。CLI 使用 `--include-partial-messages --output-format stream-json`，确实在最终 result 之前收到 content_block_delta。

真实生成测试使用 low effort 和有界输出预算；取消测试在 max effort 的首条内容到达后主动断开。未等待一小时验证硬时限；通过缩短时间尺度的单测，验证持续有数据时仍执行总时限、无数据时执行首包/空闲超时。

## 主要覆盖

- SSE 任意字符边界分片、CRLF、多行 data、中文/emoji、尾部 usage、截断 JSON 和缺少 finish_reason。
- 首个工具参数实时下发、元数据晚到、两个工具交错且下游内容块保持连续、参数 JSON 校验。
- 占位文本分片识别；交付思考后禁止静默替换；预算耗尽如实返回 max_tokens。
- 通过真实 Uvicorn/HTTP 连接验证增量交付，以及流式/非流式客户端断开后取消上游。
- 慢客户端队列有界，队列阻塞时仍可取消；仅 usage 数据不抑制下游心跳。
- 中止失败标记标签页待恢复；刷新检查在归还池槽之前完成；等待池槽时可取消。
- OpenAI / NVCF 模式的思考、正文、用量和结束事件仍可转换。

Claude Code 本次最终记录的输入/输出 tokens 为 1933/43，说明首帧占位用量能够被尾帧的实际值补充。

## 配置与边界

- 桥接默认总时限与两个启动脚本均设为 3600 秒，包含排队、准备和允许的纠正重试。
- 首个上游数据超时默认 120 秒，后续流空闲超时默认 90 秒，CDP drain 间隔默认 100ms。
- 仓库内四份 CC settings 和全局配置辅助脚本使用 API_TIMEOUT_MS=3900000；未运行辅助脚本覆盖用户现有全局配置。
- 心跳和有效增量可避免空闲断开，不会取消桥接总时限，也不能保证所有客户端都没有额外的总时限。
- 首个工具可实时输出参数；后续交错工具可能等待首个工具结束。工具之后再交错正文/思考会明确返回协议错误。
- 思考在 CC 界面的展示方式由 CLI 决定；收到进度不等于模型已经给出正文。

验证结束后，临时 HTTP 服务和测试生成标签页均已关闭；8083 不提供服务。

## 重跑

使用 Python 3.12，并按 README 配置 PYTHONPATH（本地离线依赖需要包含 vendor）：

```powershell
python tests/test_nvplay_bridge.py
python tests/test_pool.py
python tests/test_streaming.py
python -c 'import subprocess; from browser_upstream import PATCH_JS; subprocess.run(["node", "tests/browser_patch_harness.cjs"], input=PATCH_JS, text=True, check=True, timeout=15)'
```

真实 Playground 测试需要 CDP 9225 可用；脚本自行启动并关闭临时 HTTP 服务，只使用 `kstreamtest` 标签：

```powershell
python tests/live_streaming.py
```

本地运行证据保存在被 Git 忽略的 `logs/live-streaming.jsonl`、`logs/cc-streaming-smoke.jsonl`。CLI smoke 测试关闭了工具及会话持久化，没有执行工程文件编辑。
