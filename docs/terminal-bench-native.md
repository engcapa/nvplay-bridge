# CC + bridge 的非 Docker 原生评测

这套工具用于监督式实测，不是 Harbor 官方运行器，也不能产生 Artificial Analysis 官方智能指数。所有任务题面、参考解、隐藏测试、运行目录和会话日志都保存在 Git 忽略的 `logs/` 下。仓库只提交运行器、证据处理测试和不含题目内容的结果。

## 固定输入

- Terminal-Bench `v4.0.0`，提交 `452bf305c6daa62fc59061d22133a7cbc7c1572e`。
- GitHub API tarball 的 SHA256：`61530c06fc0069abc5917e386e888672e1a10950d40a061fa9c1daf52eb1935f`。
- Ubuntu base `24.04.5` amd64 原生 rootfs，SHA256：`e77b6f10c2590cef872b33ee9f635a0e3fd1f57fb074c0e52b5c7f56147a0c86`。
- 官方 npm 包 `@anthropic-ai/claude-code@2.1.110` 的 Node.js CLI。当前 CC 原生版本 2.1.321 的 Bun 路径检测需要 `/proc/self/fd`，不能在本机缺少 `/proc` 的 chroot 中工作。没有伪造 `/proc`。
- Node.js `v24.13.0`；托管 CPython `3.11.18`、`3.12.16`、`3.13.18`。
- 两档独立运行：`high`、`max`；实际下发值必须由 bridge 元数据日志复核。

## 原生基座约定

先由操作者准备 `logs/<run>/native-base`。它是解压的 Ubuntu 文件系统，使用 `chroot` 执行，不调用 Docker。所需 host 权限为 sudo、`CAP_SYS_CHROOT`、为 `/dev/null`、`zero`、`random`、`urandom` 创建字符设备的权限。此实现不要求 mount namespace。

基座中安装 `build-essential make patch git curl ca-certificates tmux file util-linux procps`；配置可用 DNS，字符设备权限为 `0666`，`/tmp` 为 `1777`。创建普通用户 `agent`、`/home/agent` 和 `/app`。配置 `policy-rc.d` 禁止包安装时启动系统服务。

将三个独立的托管 Python 目录放入 `/opt/python/3.11`、`/opt/python/3.12`、`/opt/python/3.13`；将 `uv`、Node.js 放入 `/usr/local/bin/`。将校验过的官方 CC npm 包解压到 `/opt/claude-code-2.1.110/package/`，提供入口：

```sh
#!/bin/sh
exec /usr/local/bin/node /opt/claude-code-2.1.110/package/cli.js "$@"
```

需要先在普通 UID 下完成一次实际 Bash/文件操作烟测；`claude --version` 成功不能证明工具调用可用。基座及每题安装日志应保存版本、包清单和文件摘要。托管 Python 的包安装只修改任务自己的副本，不修改 host Python。

## 执行顺序

把固定 tarball 放入 `logs/<run>/terminal-bench.tar.gz`，解压到 `logs/<run>/terminal-bench/`。启动自己的 bridge 和持续监控，再准备 `settings-high.json`、`settings-max.json`，其中包含所用 bridge 地址、模型和对应 `output_config.effort`。这些本机配置不提交 Git。

```sh
python3 tools/eval_terminal_native.py init --base logs/<run>
sudo -n python3 tools/eval_terminal_native.py prepare --base logs/<run>
sudo -n python3 tools/eval_terminal_native.py controls --base logs/<run>
sudo -n python3 tools/eval_terminal_native.py run --base logs/<run> --stage pilot --budget 1800 --repeats 1
python3 tools/report_terminal_native.py --base logs/<run> --output logs/<run>/summary.json
```

`init` 拒绝覆盖已冻结的计划；每次运行核对题目目录摘要。运行器支持六个显式环境 recipe，默认评测其中五题；其余任务需独立适配和验证。

每题先证明初始代码得 0、原始参考解得 1。隐藏测试和参考解不进入被测 agent 文件系统。CC 使用独立 UID、新会话、空 MCP、固定工具集合，禁用技能、自动压缩和自动记忆。两个 effort 分别占用不同 CPU；其判分器复用各自的 CPU 分配。原始判分器不改写，只复制任务声明的产物到全新的 verifier 文件系统。

对照失败必须先分析。环境修复后使用 `controls --retry-controls` 重跑；旧目录会完整移入 `controls-rejected/`。无效缓存对照和未完成对照都会阻止计分。模型 attempt 从不覆盖、从不自动择优重跑。

试跑完成后，使用不同 stage 执行原始 8 小时上限、三次独立重复：

```sh
sudo -n python3 tools/eval_terminal_native.py run --base logs/<run> --stage formal --budget 28800 --repeats 3
```

这条命令只覆盖计划中通过对照的题目，不代表完成全 66 题。完整数据集包含 GPU、多服务和特殊系统环境任务，必须分别适配、验证和披露缺项。

本轮按用户补充要求，将正式阶段输出上限固定为 128K（131,072），原 30 分钟试跑的实际请求上限为 32,000。正式阶段使用独立的 `settings-formal-high.json`、`settings-formal-max.json`，设置 `CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000`，并通过 `CLAUDE_CODE_EXTRA_BODY` 的 `max_tokens=131072` 固定最终请求值；同一对象中的 `output_config.effort` 分别为 high/max。CC 2.1.110 的环境变量上限按十进制 128,000 截断，请求体覆盖则可以发出 131,072；两者已在真实 CC 进程的本地协议夹具上验证，没有修改 CC 源码。

正式 bridge 使用 `BROWSER_TIMEOUT=28800`，CC 使用 `API_TIMEOUT_MS=29400000`，避免旧的一小时请求上限先于八小时 agent 预算结束长响应。正式开始前还需执行真实上游工具烟测，并从 `browser_start.max_tokens` 验证实际下发。提高输出上限不要求模型必须输出到上限；试跑与正式阶段不能合并计算。

在计划的 `formal_native_subset` 中冻结 `max_output_tokens`、`cc_output_environment_tokens`、`cc_api_timeout_ms` 后，运行器会核对正式设置文件；输出上限、effort 或客户端超时不一致会拒绝运行。每个 attempt 的 `request-protocol.json` 保存最终设置摘要和实际预算。bridge 重启后，将实际 PID、源码提交、模块摘要与超时记入 `bridge_runtime_by_stage`，避免用工具仓库最新提交冒充在途服务版本。

## 结果与限制

`result.json` 保存客户端结束状态、原始判分、超时、实际请求 effort、重试、错误和 token 用量；`identity.json` 记录运行器源文件摘要和版本。报告同时给出产物通过率和包含基础设施失败的端到端通过率，并按 stage、预算、effort 分组。未发出模型请求、判分器超时、奖励文件缺失或奖励与退出码矛盾均不能伪装成模型得分。

运行器进程初始化时冻结源码摘要、工具仓库提交及工作区状态。后续 attempt 前如果运行器或支持文件的磁盘内容发生变化，拒绝继续，需启动新进程；已加载代码不能被后来的文件编辑重新标记。实际 bridge 运行版本与评测工具版本分别记录。传输错误还区分已经交付 thinking/正文/工具内容与尚未交付内容，缺少该字段的旧日志不猜测分类。

汇总器还按 request ID 读取本机桥接元数据，统计全部上游 HTTP 错误，包括重试耗尽的最后一次错误；每次失败尝试从该 attempt 的 `browser_start` 计算到 `browser_http_error`，避免将累计请求年龄重复相加。此值包含本地标签准备、上游排队与等待响应、受限的错误正文收集，不包含之后的清理和退避；它既不是纯上游排队时间，也不是纯桥接开销。成功请求的总时长可能包含此前失败尝试，不能再与失败尝试耗时相加。缺少原始元数据或 request ID 时标为未知，不将其记为零错误。

`usage` 保留原结果文件中终态事件的用量字段合计。`transport.observed_usage` 则从完整元数据中汇总每个逻辑请求最后一次收到的 usage，补上取消前只出现在进度事件中的用量，并以 `requests_with_partial_usage` 标记尚未成功结束的观测。无 usage 的请求另计为 `requests_without_observed_usage`；未返回的用量不估算。它们都不是计费账单，尤其不能把取消前的部分观测称为完整消耗。

chroot 与上游隔离方式存在差异：共享 host 内核和网络，没有 `/proc`，没有每题强制内存/磁盘配额，Python 补丁版本及公共工具也可能不同。因此这些成绩只能标为原生适配子集成绩。上游官方方法使用不同 agent harness；不能将本结果直接放到 AA 榜单比较，也不能从五题判断两个 effort 的统计显著差异。

持续监控示例：

```sh
python3 tools/monitor_bridge.py --log logs/<run>/bridge-debug.jsonl --duration 0 --interval 5
python3 tools/monitor_bridge.py --log logs/<run>/bridge-debug.jsonl --snapshot --lookback 300
```

CC 的美元估价按第三方模型名猜测，不作为实际成本。重试中未返回的用量不能凭空补齐。全部原始证据应保留到复核结束，再按本机磁盘预算清理 rootfs。
