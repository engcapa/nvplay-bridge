# 桥接服务整体回归报告

日期：2026-11-07
范围：`nvplay_bridge.py` / `browser_upstream.py` / Brave 三标签池 / Claude Code 真实任务

> 迁移说明（2026-11-08）：本报告写于迁移前。测试脚本与 Claude Code 配置已随工程迁到 `tests/` 和 `configs/`，
> 下方命令与链接按新路径更新；当时的运行日志没有随迁移保留。
>
> 后续变更（2026-11-08）：输出上限由 65536 提升到 **131072（128K）**，下文出现的 65536 均为当时的值，
> 当前行为以 [README](../README.md) 为准。

---

## 1. 结论

| 类别 | 结果 |
|---|---|
| Bridge 单元测试 | **19/19 通过** |
| Pool 锁超时单元测试 | **1/1 通过** |
| 真实 API 回归 | **7/7 通过** |
| Tab 刷新阈值集成测试 | **通过**（1 轮/100 字符阈值验证） |
| Pool 3 并发 | **通过**（3 请求 wall≈10.0s，单请求 5.4–10.0s） |
| CC 端到端真实任务 | **成功收尾**（result=success，10 轮，94.8s，4/4 测试通过） |

CC 端到端本次**成功**，但依赖新增的“有状态工具循环断路器”：模型在上下文被压缩后会反复调用同一 Bash，断路器在第 3 次后注入停止纠正，模型才收尾。

---

## 2. 环境

- Bridge：`127.0.0.1:8083`，`UPSTREAM_KIND=browser`，CDP 9225
- 上游：`moonshotai/kimi-k3.1-pro`
- 池：3 个 `?kpool=1..3` 标签页
- `BROWSER_QUEUE_TIMEOUT=120`
- `BROWSER_TAB_MAX_TURNS=50`，`BROWSER_TAB_MAX_TEXT_LEN=500000`
- `PLACEHOLDER_RETRY=1`，`LOOP_BREAKER=1`
- CC：`kimi-k3.1-pro`，`--settings settings-kimi-high.json`，high effort

---

## 3. 单元测试

命令：

```powershell
python tests\test_nvplay_bridge.py
python tests\test_pool.py
```

覆盖：

- assistant thinking → `reasoning_content` 回传
- 未传 effort 时不设置 `reasoning_effort`
- 缺省 `max_tokens=65536`，超过则截断
- `output_config.effort=max` / `xhigh→max`
- `thinking.enabled` 预算映射
- 模型白名单 / 404
- SSE 占位符识别
- 占位符纠正重试
- thinking-only 空响应纠正重试
- 空 assistant 回合丢弃
- 占位 assistant 文本丢弃
- 连续重复短 user 消息折叠
- 重复工具 ID 唯一化与 FIFO 映射
- 无状态/有状态工具循环断路器
- 浏览器锁排队超时

结果：`Ran 19 tests ... OK`、`Ran 1 test ... OK`。

---

## 4. 真实 API 回归

命令：

```powershell
python tests\live_regression.py
```

结果：

```text
PASS models/allowlist
PASS no forced effort / default 65536
PASS output_config effort max / xhigh->max
PASS assistant thinking passback
PASS tool roundtrip
PASS SSE stream
PASS concurrency wall=10.0s individual=[10.0, 7.0, 5.4]
ALL LIVE REGRESSION PASSED
```

### Tab 刷新阈值

用独立 `ktest=1` 标签、阈值临时设为 1 轮/100 字符：

```text
[pool] mark refresh tab=ktest=1 turns=1>=1, text_len=1023>=100
request 1: sse bytes=8345
[pool] refresh tab=ktest=1 before next request
[pool] mark refresh tab=ktest=1 turns=1>=1, text_len=1023>=100
request 2: sse bytes=8364
```

验证了“标记 → 下次取用前刷新 → 计数清零”。

---

## 5. CC 端到端

任务：修复 `research/cc-e2e/final-verify/calc.py` 的 `add` bug，跑测试。

配置：

```powershell
claude --settings configs\settings-kimi-high.json `
  --model kimi-k3.1-pro --effort high `
  --allowedTools Bash Edit Read Write `
  --strict-mcp-config --mcp-config configs\mcp-empty.json `
  --disable-slash-commands --output-format stream-json --verbose `
  --dangerously-skip-permissions -p "..."
```

结果：

```json
{"subtype":"success","is_error":false,"num_turns":10,"duration_ms":94819,
 "result":"已修复 calc.py:2 ..."}
```

- 工具调用：Bash 7、Read 1、Edit 1
- `loop_breaker_stateful` 触发 **3 次**
- `calc.py` 改为 `a + b`
- 外部复跑：`Ran 4 tests ... OK`

日志：`logs/e2e-stateful-20261107-231304.jsonl`、`logs/bridge-debug.jsonl`。

### 说明

最终总结里模型写道“按你的最新指示，未再次运行测试，直接给出总结；因此修复后的测试结果尚未验证”。这是因为循环断路器的纠正消息要求“不要再次调用，直接总结”，模型照做了；修复本身已由外部测试验证 4/4 通过。

---

## 6. 发现并修复的问题

1. **强制 low**：删除默认 `reasoning_effort=low`；只在 CC 显式传 thinking/effort 时映射。
2. **thinking 丢失**：assistant `thinking` → `reasoning_content` 回传；上游接受。
3. **默认 max_tokens**：缺省 65536；超出截断。
4. **重复工具 ID**：CC 的 `Bash:0` 会跨轮复用；转换为全局唯一 ID，tool_result 按 FIFO 映射。
5. **占位回复**：`[Tool use interrupted]` 自动纠正重试；失败返回 502 而不是空。
6. **thinking-only 空回合**：自动纠正重试；历史中的空 assistant 回合丢弃。
7. **CC 恢复上下文污染**：丢弃占位 assistant 文本；折叠连续重复短 user 消息。
8. **单页面锁死**：锁排队超时 120s；`WAIT_JS` 同时要求 Send 按钮。
9. **长会话页面变重**：`BROWSER_TAB_MAX_TURNS` / `BROWSER_TAB_MAX_TEXT_LEN` 条件刷新。
10. **模型工具循环**：有状态断路器，按首条 user 消息识别会话，同一工具调用 ≥3 次注入“停止重复、直接总结”。

---

## 7. 已知限制

- 模型在上下文被 CC 压缩后仍可能重复同一工具；有状态断路器能强制收尾，但可能让模型跳过“再跑一次”这类步骤。
- 断路器按“首条 user 文本”识别会话；两个会话使用完全相同的首条 prompt 时可能共享计数。
- CC 的 `[Tool use interrupted]` 可能来自模型、CC 内部 blob 或 CC 恢复机制；桥只能减少模型侧的占位输出，无法完全控制 CC 的上下文。
- 全局 CC 设置若仍带 `CLAUDE_CODE_EXTRA_BODY=effort=max`，所有请求都会走 max；测试 high 档使用的是 `settings-kimi-high.json`。

---

## 8. 相关文件

- [nvplay_bridge.py](../nvplay_bridge.py)
- [browser_upstream.py](../browser_upstream.py)
- [test_nvplay_bridge.py](../tests/test_nvplay_bridge.py)
- [test_pool.py](../tests/test_pool.py)
- [live_regression.py](../tests/live_regression.py)
- [settings-kimi-high.json](../configs/settings-kimi-high.json)
- 历史运行日志（`e2e-stateful-20261107-231304.jsonl` 及当时的调试日志）未随迁移保留，现有日志写入 [logs/bridge-debug.jsonl](../logs/bridge-debug.jsonl)
