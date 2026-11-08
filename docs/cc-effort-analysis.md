# CC 推理档位与“工具中断”深入分析

日期：2026-11-07
对象：Claude Code 2.1.319 → `nvplay_bridge`（browser/Brave）→ Kimi K3.1 Pro

---

## 1. 结论速览

| 问题 | 结论 |
|---|---|
| 最初 CC 测试用的推理档位 | **low**。`--model sonnet` 是未识别别名，CC 没有发送 `thinking`/`output_config`，bridge 默认 `reasoning_effort=low` |
| 完整 Sonnet 模型 | CC 发送 `thinking.budget_tokens=31999`；修正映射后为 **max** |
| Opus 4.5 + `--effort` | CC 发送 `output_config.effort="high"` + thinking 31999；实际 **high**（`--effort low/xhigh/max` 都被压成 high） |
| 强制 max | `CLAUDE_CODE_EXTRA_BODY='{"output_config":{"effort":"max"}}'` → 请求带 `output_config.effort=max`，bridge 映射 **max** |
| `[Tool use interrupted]` | **不是桥/CLI 的中断**，而是模型自己输出的 assistant 文本；工具调用已正常执行 |
| 复测（max） | 8 轮、117s，`calc.py` 修复，4/4 测试通过；没有再出现该文本 |

---

## 2. 抓到的 CC 请求（bridge debug log）

### 2.1 初次 `--model sonnet`

```json
{"event":"request","thinking":null,"effort":null,"output_config":null,
 "explicit_effort":null,"max_tokens":32000}
{"event":"payload","reasoning_effort":"low","requested_effort":null}
```

CC 把 `sonnet` 当成未识别模型（`[claude-code:unrecognized_model]`），没有发 thinking/effort；bridge 走默认 **low**。

### 2.2 完整 Sonnet 模型

```json
{"event":"request","thinking":{"budget_tokens":31999,"type":"enabled","display":"updates"},
 "output_config":null}
```

CC 只发 thinking budget，不发 `output_config`。原映射 31999→high；现已修正为 **max**。

### 2.3 Opus 4.5 + `--effort`

`--effort low`、`--effort xhigh`、`--effort max` 三次请求全部是：

```json
{"thinking":{"budget_tokens":31999,"type":"enabled"},
 "output_config":{"effort":"high"},
 "explicit_effort":"high"}
{"payload":"reasoning_effort":"high"}
```

CC 的 `--effort` 没有按预期变成 low/xhigh/max，而是固定 high。可能原因：

- 用户 `~/.claude/settings.json` 的 `effortLevel: "xhigh"` / `env.CLAUDE_CODE_EFFORT_LEVEL: "max"` 优先；
- 或 CC 对 Opus 4.5 把 effort 上限压到 high。

### 2.4 强制 max

`settings-effort-max.json` 里加：

```json
"CLAUDE_CODE_EXTRA_BODY": "{\"output_config\":{\"effort\":\"max\"}}"
```

抓到的请求变成：

```json
{"output_config":{"effort":"max"},"explicit_effort":"max"}
{"payload":"reasoning_effort":"max"}
```

因此 **adaptive effort 可以直接走 `output_config.effort`，不需要也不应该从模型名或 thinking 猜**。

---

## 3. Anthropic 最新接口依据

来自官方 Python SDK 类型（`anthropic-sdk-python`）：

```python
class OutputConfigParam(TypedDict, total=False):
    effort: Optional[Literal["low", "medium", "high", "xhigh", "max"]]
    format: Optional[JSONOutputFormatParam]

class ThinkingConfigAdaptiveParam(TypedDict, total=False):
    type: Required[Literal["adaptive"]]
    display: Optional[Literal["summarized", "omitted"]]

ThinkingConfigParam = Union[
    ThinkingConfigEnabledParam,
    ThinkingConfigDisabledParam,
    ThinkingConfigBetweenToolsParam,
    ThinkingConfigAdaptiveParam,
]
```

即：

- `output_config.effort` 是标准 effort 入口，含 **xhigh**；
- `thinking.type` 支持 **adaptive**，由模型自行决定思考量；
- `thinking.enabled.budget_tokens` 是手动预算，和 effort 是两套入口。

---

## 4. bridge 的映射实现

当前 `nvplay_bridge.py`：

```text
output_config.effort
  → 直接映射 low/medium/high/xhigh/max
  → xhigh 归一到 max（上游 Playground 只支持 low/medium/high/max）

effort / reasoning_effort / X-Reasoning-Effort header
  → 同样直接映射

thinking.type == adaptive
  → 有 output_config.effort 时以 effort 为准
  → 没有时走默认 low

thinking.type == enabled
  → budget 兜底：<=2048 low, <=8192 medium, <=16384 high, 其余 max
  → 31999 现在映射 max（之前误映射 high）
```

验证（直接 HTTP）：

| 请求 | 结果 |
|---|---|
| `output_config.effort=max` | 200，`reasoning_effort=max` |
| `thinking.adaptive` + `output_config.effort=xhigh` | 200，`reasoning_effort=max` |
| `CLAUDE_CODE_EXTRA_BODY` → max | CC 请求带 `output_config.effort=max`，bridge max |

---

## 5. “调用工具中断”到底是什么

最初那次日志：

```json
{"type":"assistant",
 "message":{"id":"msg_83c702d269d04fd086854662","model":"sonnet",
            "content":[{"type":"text","text":"[Tool use interrupted]"}],
            "stop_reason":"end_turn"}}
{"type":"result","is_error":false,"terminal_reason":"completed",
 "result":"[Tool use interrupted]"}
```

判断依据：

1. **消息 ID 是 bridge 生成的**：`msg_` + 24 位 hex，来自 `stream_anthropic()`，不是 CC 的 `<synthetic>` 消息；
2. **`model` 是 `sonnet`**，不是 `<synthetic>`（此前 `model_not_found` 的合成错误明确是 `<synthetic>`）；
3. **`is_error:false`、`terminal_reason:"completed"`**，不是工具错误或流错误；
4. 后续 debug 运行里没有任何 `browser_error`，且有 **61.8 秒**的回合成功完成；
5. 复测中模型 thinking 自己写道 “continue from the interrupted state … perhaps interruption harness”，说明它**把上一轮的空结束误认为中断**，然后输出了这个占位符。

所以：**工具并没有被中断**；Bash/Read/Edit 都已执行并返回结果。是模型在多次 Edit 失败、连续 Read 后放弃了，输出了 `[Tool use interrupted]` 这段文本，CC 把它当作普通 assistant 文本结束。

### 仍然存在的理论风险

CC 二进制里确实有：

- `CLAUDE_STREAM_IDLE_TIMEOUT_MS`
- `CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS`
- `CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS`

而当前桥是**缓冲式**：完整生成后才回传，CC 在生成期间收不到字节。如果某轮超过 CC 的流超时阈值，理论上会出现真正的超时中断。已观察到的 61.8s 回合没有触发；本次 `[Tool use interrupted]` 也不是这个原因。若后续出现 `is_error:true` / `terminal_reason` 为错误，再按流超时排查（可调大上述环境变量）。

---

## 6. 复测（max effort）

- 任务：修复 `calc.py` 的 `add` bug，跑测试。
- 配置：`claude-opus-4-5`、`CLAUDE_CODE_EXTRA_BODY` 强制 `output_config.effort=max`、核心工具。
- 结果：8 轮、117s，`calc.py` 修正为 `a + b`，`python -m unittest -v` **4/4 OK**。
- 每次请求的 payload 都是 `reasoning_effort=max`；没有 `browser_error`，没有再输出 `[Tool use interrupted]`。
- 但模型最后仍以一句“我将重新运行…”结束，没有按提示输出总结——**收尾执行力弱**是模型问题，不是桥问题。

日志：`logs/retest-max-20261107-203900.jsonl`、`logs/bridge-debug.jsonl`。

## 6.1 模型白名单：CC 直接指定 `kimi-k3.1-pro`

已改为：

- 上游固定 `UPSTREAM_MODEL=moonshotai/kimi-k3.1-pro`；
- 请求里的 model 只做别名白名单；
- 不在白名单直接 **404 not_found_error**。

CC 配置 `settings-kimi.json`：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8083",
    "ANTHROPIC_AUTH_TOKEN": "test",
    "ANTHROPIC_MODEL": "kimi-k3.1-pro",
    "CLAUDE_CODE_ALLOW_OTHER_MODELS": "1",
    "CLAUDE_CODE_EXTRA_BODY": "{\"output_config\":{\"effort\":\"max\"}}"
  },
  "model": "kimi-k3.1-pro"
}
```

实测：

```json
{"event":"request","requested_model":"kimi-k3.1-pro",
 "upstream_model":"moonshotai/kimi-k3.1-pro",
 "thinking":{"type":"adaptive","display":"updates"},
 "output_config":{"effort":"max"}}
{"event":"payload","model":"moonshotai/kimi-k3.1-pro",
 "reasoning_effort":"max"}
```

CC 结果：`KIMI CC`，HTTP 200。`[unrecognized_model]` 警告仍会出现，但请求确实发出。

不支持的模型（如 `claude-sonnet-4-5-20250929`）：

```json
{"event":"model_rejected","model":"claude-sonnet-4-5-20250929",
 "allowed":["kimi-k3.1-pro","kimi-k3_1-pro",
            "moonshotai/kimi-k3.1-pro","moonshotai/kimi-k3_1-pro"]}
```

CC 收到 404，显示 `model_not_found` / “There's an issue with the selected model…”。

所以：**CC 可以指定非 Claude 的模型 ID；适配层只接受 Kimi 别名，其它一律拒绝，上游永远是 `moonshotai/kimi-k3.1-pro`。**

---

## 7. 建议

1. **需要 max 时**：给 CC 加
   `CLAUDE_CODE_EXTRA_BODY='{"output_config":{"effort":"max"}}'`，不要依赖 `--effort` 或模型名。
2. **不要从 thinking budget 猜档位**：优先 `output_config.effort`；thinking 仅作兼容兜底。
3. 对 CC 的工具循环，仍然需要**外部测试门禁**：模型可能随时输出一段文字就结束。
4. 如果怀疑真正的流超时，再设置/调大 `CLAUDE_STREAM_IDLE_TIMEOUT_MS`、`CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS`，并对照 `is_error`/`terminal_reason` 判断。
5. `[Tool use interrupted]` 这类文本要结合 `model` 字段和消息 ID 判断是模型输出还是 CLI 合成，不要直接当成传输故障。
