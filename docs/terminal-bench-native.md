# CC + bridge 的非 Docker 原生评测

这套工具用于监督式实测，不是 Harbor 官方运行器，也不能产生 Artificial Analysis 官方智能指数。所有任务题面、参考解、隐藏测试、运行目录和会话日志都保存在 Git 忽略的 `logs/` 下。仓库只提交运行器、证据处理测试和不含题目内容的结果。

2026-11-11 起按用户要求使用当前普通用户 PATH 中的 Claude Code，切换时为 **2.1.322**，具体见下文“当前 PATH 原生 CC”。2.1.110 仅保留为历史协议与基座的兼容组件，新的计分会话使用冻结的 PATH 原生可执行文件。

## 固定输入

- Terminal-Bench `v4.0.0`，提交 `452bf305c6daa62fc59061d22133a7cbc7c1572e`。
- GitHub API tarball 的 SHA256：`61530c06fc0069abc5917e386e888672e1a10950d40a061fa9c1daf52eb1935f`。
- Ubuntu base `24.04.5` amd64 原生 rootfs，SHA256：`e77b6f10c2590cef872b33ee9f635a0e3fd1f57fb074c0e52b5c7f56147a0c86`。
- 历史阶段使用官方 npm 包 `@anthropic-ai/claude-code@2.1.110` 的 Node.js CLI；新阶段冻结当前 PATH 原生 CLI 的版本、文件大小与 SHA256。原生 Bun 需要真实 `/proc/self/fd`，不能使用没有 `/proc` 的旧 chroot 启动方式。
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

## 当前 PATH 原生 CC

容器允许 chroot，但不允许 mount namespace 或挂载 proc。原生 CLI 通过 PRoot 5.5.0 转换路径并绑定真实 `/proc`；系统包 PRoot 5.1.0 的 statx 兼容性不足，不能使用。构建源固定为 `https://github.com/proot-me/proot.git` 的 `v5.5.0`、提交 `25dc6a3134891f98a79f57ce1c2c1b23ff15cad1`；通过 `git -c http.proxy=http://127.0.0.1:31128 clone --depth 1 --branch v5.5.0 …` 获取源码，安装 `libtalloc-dev` 后执行 `make -C <source>/src -j4`，将结果安装到 `/usr/local/libexec/nvplay-proot-5.5.0`。apt 的 http/https Acquire 代理均使用 `http://127.0.0.1:31128`。

完成原有下载、基座、模板及计划恢复后，在启动 smoke/campaign 前冻结当前 CLI。`command -v` 必须由普通用户 shell 展开，以免选中 sudo 的不同 PATH：

```sh
sudo -n python3 tools/configure_terminal_native_path_cc.py \
  --base logs/<run> --claude "$(command -v claude)" \
  --proot /usr/local/libexec/nvplay-proot-5.5.0 \
  --proot-source-commit 25dc6a3134891f98a79f57ce1c2c1b23ff15cad1 \
  --stage formal-1m-720k-path-cc-<new-run>
```

该步骤拒绝运行中的控制器和未归档的在途任务，保留已完成位置及旧协议，建立独立的新阶段。基座中的旧 CLI 入口会在每个新 agent root 内指向 PATH 快照；新阶段不加载 Node 网络适配器。`API_TIMEOUT_MS=29400000` 保留，单个正式会话没有墙钟截止，启用压缩时同时传入 `--autocompact 720000`。完成版本切换后仍必须通过新的 high/max 真实工具 smoke，旧版 smoke 不能代替。

每个任务在 `/var/tmp/nvplay-bridge-cc-<uid>-<random>/root` 建立独立文件系统，记录到 `runtime-root.json`。先用 `setpriv` 降为真实 UID 20000–29999，清空附加组并启用 `no-new-privs`，再启动 PRoot；host cwd 设在该任务 root 内。PRoot 仅负责路径转换，隔离依赖真实 UID 和目录权限：题库、隐藏测试、参考解、判分 root 和其它任务必须不可被该 UID 读取。本轮 `/home/zhyhang` 为 0750，独立 UID 的实际读取权限检查通过；真实 `/proc` 会暴露部分 host 元数据，因此不能声称与官方容器隔离等同。

CC 自然结束或行政停止后，先确认其 UID 已无活进程，再将外部 root 移回标准 `agent-root`，交给原判分与校验归档流程。`stop_terminal_native_at.py` 同时识别外部 root 和真实 UID；替换停机工具后要按原绝对截止时间重启计时器。重启不能复用旧 PID、外部 root 或会话。

## 可提交的环境重建与持续检查点

`tools/provision_terminal_native.py` 将下载校验、原生基座与离线 wheel 准备保存在源码中。Ubuntu 与数据集、官方 CC 包、CC CLI 和 Node 二进制均检查固定 SHA256；Python 使用确切补丁版本。apt 包及 wheel 的实际版本和摘要另存 manifest，不假设软件源与上一轮完全相同。

```sh
python3 tools/provision_terminal_native.py downloads --base logs/<run>
HTTPS_PROXY=http://127.0.0.1:31128 python3 -m pip download --no-deps --only-binary=:all: --dest logs/<run>/downloads uv==0.13.7
sudo -n python3 tools/provision_terminal_native.py base --base logs/<run> --node /path/to/node-v24.13.0 --uv-wheel logs/<run>/downloads/uv-0.13.7-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl
python3 tools/provision_terminal_native.py wheels --base logs/<run>
python3 tools/start_terminal_native_services.py --base logs/<run> --brave .brave/brave
sudo -n python3 tools/watch_checkpoint_terminal_native.py --base logs/<run> --output docs/evidence/<checkpoint>.json --commit
```

定时进程每 10 秒检查一次实质进展（阶段、工具调用、请求里程碑或出分），有变化即保存；距最近一次 checkpoint 满 5 分钟也会补存。控制器的即时保存会重置这 5 分钟。

用户要求远端持续保存时，在 checkpoint 命令增加 `--push`，在 campaign 命令增加 `--push-checkpoints`。两者使用相同权限写入共享的本地日志；Git 提交和推送仍使用仓库所有者身份。每次提交后立即经指定代理推送；网络推送失败会保留本地提交，写入私有 `checkpoint-git-sync.json` / `checkpoint-push.log`，定时进程会重试，不终止在途 CC。

2026-11-10 的新上下文阶段使用 `configure --cc-model 'kimi-k3.1-pro[1m]' --auto-compact-window 720000 --no-agent-timeout --continue-checkpoint docs/evidence/cc-bridge-resume-2026-11-10.json`。模型、压缩开关和窗口由计划与设置共同校验，实际启动不再强制禁用压缩；旧阶段继续保留原配置。`--no-agent-timeout` 将 `agent_wall_clock_timeout_seconds` 显式冻结为 null，CC 会话等待自然结束；旧的 `budget_seconds=28800` 只作原协议参考，结果与请求协议同时记录实际超时策略，续跑拒绝混用两种策略的已完成结果。请求级 bridge/API 超时与原始 verifier 限制另行保留。

续跑计划冻结所有缺失位置，保留旧 checkpoint 中已完成的成功和失败，单独报告新阶段成绩。原始日志已丢失的未完成任务不能恢复或补分，必须明确登记后用新会话重跑。

`tests/probe_cc_context.py` 使用未修改的固定 CC 和本地 Messages 夹具，验证 1M 窗口报告、压缩阈值附近的行为、关闭压缩对照及实际请求模型。它模拟 usage，不能证明上游实际容纳了等量输入；真实 smoke 另验证 high/max 工具闭环与实际窗口。

服务启动器拒绝占用已有端口，显式使用外网代理和八小时、128K 配置；它不替换正在工作的服务。首次工具烟测创建标签池后，另启动 `tools/monitor_cdp_network.py` 持续观测网络终态。checkpoint 仅归档协议、版本、统计、产物摘要及证据哈希，隐藏 CC 正文和错误响应正文；`--commit` 每次只提交指定检查点文件。

准备好服务及 Node 安装树后，`python3 tools/provision_terminal_native.py configure --base logs/<run>` 校验实际 `/health`、历史任务摘要及网络适配器，生成正式设置文件，并一次性冻结新计划。`sudo -n python3 tools/smoke_terminal_native.py --base logs/<run>` 使用两个普通 UID，运行 high/max 的真实 Bash/Python 随机文件 SHA256 闭环，独立核对文件及最终回答；此烟测不计入题目分数。

运行器 `run --resume` 保留所有已完成结果，包括失败；协议不符会拒绝续跑。容器重启后可显式使用 `--resume --recover-interrupted`：只有原控制器和 agent 均已停止，才将未完成目录移入 `runs-interrupted/`，记为 `score=null` 并排除模型判分，然后使用新会话重跑对应计划位置。原日志不覆盖。PID 身份同时检查 boot ID 和进程起始时刻，避免重启后误认复用 PID。恢复时仍需重新启动服务、记录实际服务身份并完成必要烟测。

若文件系统不支持 reflink，可运行 `sudo -n python3 tools/archive_terminal_native_roots.py --base logs/<run>`。它只处理已结束的任务、有效对照和烟测，拒绝仍被进程使用的 rootfs；先用 tar/gzip 保存完整文件树，再运行 tar compare 核对原目录、计算 SHA256，最后回收展开目录。日志、判分、原始产物及压缩 rootfs 均保留，可按各 `*-root-archive.json` 的命令还原。不要清理仍在运行的 rootfs 或模板。

`tools/run_terminal_native_campaign.py` 将模板准备、对照、烟测和正式 30 次串联起来；每次完成立即保存并提交 checkpoint，再核验压缩 rootfs。它复用同一运行器及原始判分器，两个 effort 仍按题配对运行，单次预算保持八小时。Git 操作使用仓库所有者身份，避免 root 创建不可写的 Git 对象。

```sh
sudo -n python3 tools/run_terminal_native_campaign.py --base logs/<run> --checkpoint-output docs/evidence/<checkpoint>.json
# 容器重启、服务恢复并记录新身份后：
sudo -n python3 tools/run_terminal_native_campaign.py --base logs/<run> --checkpoint-output docs/evidence/<checkpoint>.json --resume --recover-interrupted
```

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

每题先证明初始代码得 0、原始参考解得 1。隐藏测试和参考解不进入被测 agent 文件系统。CC 使用独立 UID、新会话、空 MCP、固定工具集合，禁用技能和自动记忆；旧阶段禁用自动压缩，新上下文阶段按冻结计划启用。两个 effort 分别占用不同 CPU；其判分器复用各自的 CPU 分配。原始判分器不改写，只复制任务声明的产物到全新的 verifier 文件系统。

对照失败必须先分析。环境修复后使用 `controls --retry-controls` 重跑；旧目录会完整移入 `controls-rejected/`。无效缓存对照和未完成对照都会阻止计分。模型 attempt 从不覆盖、从不自动择优重跑。

试跑完成后，使用不同 stage 执行原始 8 小时上限、三次独立重复：

```sh
sudo -n python3 tools/eval_terminal_native.py run --base logs/<run> --stage formal-128k-fixed --budget 28800 --repeats 3
```

这条命令只覆盖计划中通过对照的题目，不代表完成全 66 题。完整数据集包含 GPU、多服务和特殊系统环境任务，必须分别适配、验证和披露缺项。

本轮按用户补充要求，将正式阶段输出上限固定为 128K（131,072），原 30 分钟试跑的实际请求上限为 32,000。正式阶段使用独立的 `settings-formal-high.json`、`settings-formal-max.json`，设置 `CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000`，并通过 `CLAUDE_CODE_EXTRA_BODY` 的 `max_tokens=131072` 设置请求值；同一对象中的 `output_config.effort` 分别为 high/max。CC 2.1.110 的环境变量上限按十进制 128,000 截断，请求体覆盖则可以发出 131,072；两者已在真实 CC 进程的本地协议夹具上验证，没有修改 CC 源码。

断流探针进一步发现，两版 CC 的非流式回退都会覆盖 EXTRA_BODY，将上限降到 64,000。正式 bridge 因此还必须显式配置 `BROWSER_FIXED_MAX_TOKENS=131072`，并从 `/health.browser_output_tokens` 和实际请求日志复核。已启动的原 `formal` 首对保留为修复前补充观察，不计入新的 30 次 `formal-128k-fixed`；这一调整在原首对出分前冻结。

正式 bridge 使用 `BROWSER_TIMEOUT=28800`、`BROWSER_FIRST_BYTE_TIMEOUT=3600`、`BROWSER_IDLE_TIMEOUT=3600`，CC 使用 `API_TIMEOUT_MS=29400000`。原 90 秒空闲上限曾在已有输出后终止两个真实慢响应；它与首字节、总时限是不同约束。`/health.browser_timeouts` 公布实际加载值。正式开始前还需执行真实上游工具烟测，并从 `browser_start.max_tokens` 验证实际下发。提高输出上限不要求模型必须输出到上限；不同阶段不能合并计算。

Node fetch 的默认等响应头限制还会在约 301 秒先于 SDK 超时取消非流式回退；真实 CC 本地探针捕获 `UND_ERR_HEADERS_TIMEOUT`。本轮为两个 effort 一致启用显式 Node 适配器，安装方法为 `npm ci --ignore-scripts --prefix tools/cc_node_runtime`。在计划 `formal_native_subset.node_network_adapter` 冻结完整安装树的 `tree_sha256`；运行器开始 attempt 前核对摘要，将独立副本放入 chroot，并用 `node --require` 启动未修改的官方 `cli.js`。启动前向进程注入 API_TIMEOUT_MS 和 ANTHROPIC_BASE_URL，避免只写 settings 而错过预加载阶段。没有使用全局 NODE_OPTIONS，因此模型的后续 Node 工具命令不会自动加载适配器。

适配器只调整配置 API origin 的 header/body 超时，取消信号及其他 origin 保持原行为。安装依赖、预加载源码、实际启动命令和配置摘要写入版本身份及 `request-protocol.json`；历史 stage 不自动启用。该适配改变客户端运行环境，属于与官方 harness 的另一项明确差异，不宣称修改了 CC 或解决了提供方 HTTP/2 中断。

在计划的 `formal_native_subset` 中冻结 `max_output_tokens`、`cc_output_environment_tokens`、`cc_api_timeout_ms` 后，运行器会核对正式设置文件；输出上限、effort 或客户端超时不一致会拒绝运行。每个 attempt 的 `request-protocol.json` 保存最终设置摘要和实际预算。bridge 重启后，将实际 PID、源码提交、模块摘要与超时记入 `bridge_runtime_by_stage`，避免用工具仓库最新提交冒充在途服务版本。

## 结果与限制

`result.json` 保存客户端结束状态、原始判分、超时、实际请求 effort、重试、错误和 token 用量；`identity.json` 记录运行器源文件摘要和版本。报告同时给出产物通过率和包含基础设施失败的端到端通过率，并按 stage、预算、effort 分组。未发出模型请求、判分器超时、奖励文件缺失或奖励与退出码矛盾均不能伪装成模型得分。

运行器进程初始化时冻结源码摘要、工具仓库提交及工作区状态。后续 attempt 前如果运行器或支持文件的磁盘内容发生变化，拒绝继续，需启动新进程；已加载代码不能被后来的文件编辑重新标记。实际 bridge 运行版本与评测工具版本分别记录。传输错误还区分已经交付 thinking/正文/工具内容与尚未交付内容，缺少该字段的旧日志不猜测分类。

汇总器还按 request ID 读取本机桥接元数据，统计全部上游 HTTP 错误，包括重试耗尽的最后一次错误；每次失败尝试从该 attempt 的 `browser_start` 计算到 `browser_http_error`，避免将累计请求年龄重复相加。此值包含本地标签准备、上游排队与等待响应、受限的错误正文收集，不包含之后的清理和退避；它既不是纯上游排队时间，也不是纯桥接开销。成功请求的总时长可能包含此前失败尝试，不能再与失败尝试耗时相加。缺少原始元数据或 request ID 时标为未知，不将其记为零错误。

`usage` 保留原结果文件中终态事件的用量字段合计。`transport.observed_usage` 则从完整元数据中汇总每个逻辑请求最后一次收到的 usage，补上取消前只出现在进度事件中的用量，并以 `requests_with_partial_usage` 标记尚未成功结束的观测。无 usage 的请求另计为 `requests_without_observed_usage`；未返回的用量不估算。它们都不是计费账单，尤其不能把取消前的部分观测称为完整消耗。

原生适配与上游隔离方式存在差异：共享 host 内核和网络，旧 chroot 没有 `/proc`、新 PRoot 绑定真实 `/proc`，没有每题强制内存/磁盘配额，Python 补丁版本及公共工具也可能不同。因此这些成绩只能标为原生适配子集成绩。上游官方方法使用不同 agent harness；不能将本结果直接放到 AA 榜单比较，也不能从五题判断两个 effort 的统计显著差异。

持续监控示例：

```sh
python3 tools/monitor_bridge.py --log logs/<run>/bridge-debug.jsonl --duration 0 --interval 5
python3 tools/monitor_bridge.py --log logs/<run>/bridge-debug.jsonl --snapshot --lookback 300
```

CC 的美元估价按第三方模型名猜测，不作为实际成本。重试中未返回的用量不能凭空补齐。全部原始证据应保留到复核结束，再按本机磁盘预算清理 rootfs。

传输修复需要分阶段部署时，可用 `tools/advance_terminal_native_transport.py` 等待在途会话自然结束，核对已验证模块、保存成绩、归档、切换服务并在实际部署重做工具烟测，再继续冻结的剩余位置。`formal_native_subset.preserved_local_schedule` 保留本目录早期阶段已完成的位置（包括失败），checkpoint 的计划总数为这部分加剩余 schedule；历史阶段协议保存在 `protocol_history`，结果仍按原 stage 分组。该控制器不会中断 CC，也不会覆盖或择优重跑成绩。使用前应检查记录的控制器、部署与原始目录是否仍在；部分部署失败需先诊断。

`clone_root` 会先解析基座根目录别名再复制，保留 rootfs 内部链接；拒绝已有目标（含悬空链接）。克隆输出必须具有独立 inode，不能把目录链接当成隔离副本。
