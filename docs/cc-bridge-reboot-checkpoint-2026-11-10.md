# CC + bridge 评测重启检查点（2026-11-10）

**已于 2026-11-10 16:47（北京时间）收到用户继续评测的指令并恢复工作。** 本文保留重启前交接；新批次状态、修复与结果见 [重启后评测记录](cc-bridge-resumed-benchmark-2026-11-10.md) 和 [持续检查点](evidence/cc-bridge-resume-2026-11-10.json)。

## 已持久保存的内容

- [中文实测报告](cc-bridge-native-benchmark-2026-11-10.md)：逐阶段结果、排查修复过程及实际效果。
- [脱敏结果与证据摘要](evidence/cc-bridge-native-2026-11-10.json)：12 次完整观察、2 次用户中止、分组统计、版本身份、产物摘要、判分对照、16 个断流探针、延迟响应头及真实上游烟测结果，并附原证据文件的 SHA256 和大小。
- [原生环境与运行说明](terminal-bench-native.md)：固定数据集、软件、chroot 隔离、依赖、运行器及正式协议。
- [taomni 工程实测报告](cc-live-validation-2026-11-09.md)：CC 经 bridge 完成实际产品修改和跨平台 UI 验证的证据。

原始 `logs/eval-aa-20261110/` 被 Git 忽略，包含会话、rootfs、判分日志等，不保证清理后仍在。归档没有复制基准题面、参考解、隐藏测试、CC 解题正文或凭据。**哈希无法恢复已删除文件；保存的是结果、证据摘要与重新构建方法，历史运行不能完整重放。**

## 暂停时的确切状态

2026-11-10 13:45:52（北京时间）按用户要求终止在途正式任务，阻止下一题启动，也没有对部分产物判分。

| 阶段 | 结果与状态 |
|---|---|
| `pilot`，1800 秒，上限 32000 | 10 次完成；high 产物 2/5、端到端 1/5；max 均为 0/5 |
| `formal`，28800 秒，修复前补充观察 | 2 次完成；同一道题 high 2/7、产物 0；max 7/7、产物 1；两档均因客户端/传输错误退出，端到端失败 |
| `formal-128k-fixed`，28800 秒，固定 131072 | 完成 0/30；两次用户中止，28 次未启动；暂无正式成绩 |

两次用户中止均运行约 1060.82 秒，各调用 10 次工具、发出 4 个逻辑请求，其中 3 个完成、1 个按暂停要求取消。没有 HTTP 错误、重试或终止错误，取消后无遗留请求。状态为 `administratively_interrupted`、`score=null`、`excluded_from_model_grades=true`。它们不计模型失败，也不能当作成功完成。

评测控制器、本轮独立启动的 :8083 bridge、两个监控进程及 :9225 Brave 均已停止。停机前 health 为正常，5 个标签均 ready。**重启后不要复用旧 PID、控制器状态或 CC 会话。**

## 已完成的修复与验证

| 事项 | 保存的结论 |
|---|---|
| CC 非流式回退将输出上限降为 64000 | 显式 `BROWSER_FIXED_MAX_TOKENS=131072` 修复；两版 CC × 四种截断 × 开关，共 16 个契约 case 通过；未完成工具执行 0 次 |
| Node CC 等响应头约 301 秒取消 | 真实探针捕获 `UND_ERR_HEADERS_TIMEOUT`；对 API origin 单独调整超时的适配器，最终真实 CC 等到 310 秒延迟响应，一次回退成功，总耗时 311.557 秒 |
| 慢上游触发原 90 秒 idle 限制 | 新正式协议的首字节、idle 均为 3600 秒，总时限 28800 秒；这是明确的预算调整 |
| 请求错误、重试、取消前用量与版本归属 | 已补齐统计、用量观测、运行器源码冻结及实际服务身份记录 |
| CDP 恢复期间 10 次连接超时 | 旧退避机制自行恢复；新增 phase/method/type 诊断，根因仍未知 |
| 真实 NVIDIA HTTP 200/h2 后断流 | 捕获 `net::ERR_HTTP2_PROTOCOL_ERROR`，`canceled=false`；提供方、代理或链路责任点仍未确定 |
| 修复版实际部署 | 源码 `9de90ac3107c3702c63a89b87771d57fb582a14f`；两档真实 NVIDIA 工具闭环通过，high 90.011 秒、max 52.701 秒，4 个请求全部为 131072 |
| 已完成回归 | Python 176 项通过，16.279 秒；Node 网络行为 1 项通过，覆盖六条请求路径 |

关键代码提交为 `d258200`（固定上限与 Node 适配器）、`9de90ac`（CDP 诊断）、`cac83d6`（运行器身份及协议校验）；更早的原生运行器与监控修复已在 main。部署状态以逐阶段身份和模块摘要为准，不能用后来生成报告的提交冒充实际运行版本。

旧控制器在源码变化后报 `runner source changed after initialization`，按设计拒绝下一题。这是身份保护生效；之后已用新控制器启动修复版，不记为 bridge 缺陷。

## 重启后按顺序恢复

1. 用户确认重启完成后，先核对 main 和归档文件仍在；建立新的 `logs/<new-run>/`，不覆盖旧证据。检查端口、代理、磁盘、内存、sudo/chroot 权限及本机工具。禁止 Docker；缺失的本机依赖可以 apt 安装。
2. 按原生说明恢复固定数据集与基座。Terminal-Bench 提交固定为 `452bf305c6daa62fc59061d22133a7cbc7c1572e`，tarball SHA256 为 `61530c06fc0069abc5917e386e888672e1a10950d40a061fa9c1daf52eb1935f`。继续使用官方 Node CC 2.1.110、Node 24.13.0 和相同 Python 版本；不要把依赖 `/proc` 的原生 CC 2.1.321 直接放入原 chroot。
3. 新建冻结计划，保持五题：`session-window-debug`、`embedding-drift-monitor`、`batched-eval-parity`、`mvcc-lsm-compaction`、`ks-solver-cpp`。不因既有成绩删换题；WAL 因原始参考解/判分组合不稳定而排除的记录继续保留。只运行必要的准备、对照和烟测，不必重复已结束的短预算试跑。
4. 重新执行初始代码 0 分、原始参考解 1 分的对照。校验隐藏测试与参考解没有进入 agent 文件系统；继续使用独立 UID、独立 agent/verifier rootfs、新会话和固定工具集合。模型作答期间不提供既有诊断、参考解或判分反馈。
5. 用 `npm ci --ignore-scripts --prefix tools/cc_node_runtime` 恢复适配器安装树。预期 SHA256 为 `2a6ca0d5c23145f875c4b65c50645c1480526f86e45d7b455542b597e504716f`，208 文件；由运行器 `network_tree_identity` 核对。若摘要变化，先解释与记录，不能伪造匹配。将配置写入 `formal_native_subset.node_network_adapter`。
6. 自行启动独立 Brave 和新 bridge，实际加载下表配置并启动持续监控。`start-bridge.sh` 当前的 `REQUEST_TIMEOUT` 默认 3600 秒，会写入 `BROWSER_TIMEOUT`；不能仅在外层设置环境变量就声称脚本使用八小时。恢复时应使用明确配置的启动命令，或先核对启动脚本配置。从 `/health` 及实际 `browser_start` 记录核验，不只读磁盘设置。
7. 在普通 agent UID 下重新做 high/max 真实上游 Bash/文件摘要闭环，检查 effort、固定 131072、错误及标签清理。保存新服务和运行器源码身份；如果新环境需要改动，先完成回归与协议记录，再开始计分。
8. 在首个新计分 attempt 前记录替代关系：旧两次因用户重启中止，排除成绩；新日志目录作为完整正式 30 次的重新开始，五题 × 两档 × 各三次。保留原 12 次观察；新结果不与旧预算混算，也不择优覆盖。
9. 使用新控制器执行正式阶段并监控。两个 effort 的限制保持一致；没有新证据时不要因为等待慢而提前中止。若发现缺陷，保存失败证据、独立审查、修复和复测，按事先记录的规则区分协议阶段。

| 正式参数 | 固定值 |
|---|---|
| 模型 | `moonshotai/kimi-k3.1-pro` |
| effort | `high`、`max`，各三次独立重复 |
| 单次预算 / `BROWSER_TIMEOUT` | 28800 秒 |
| `BROWSER_FIRST_BYTE_TIMEOUT` / `BROWSER_IDLE_TIMEOUT` | 均为 3600 秒 |
| `BROWSER_MAX_TOKENS` / `BROWSER_FIXED_MAX_TOKENS` | 均为 131072 |
| `API_TIMEOUT_MS` | 29400000，预加载适配器前注入 CC 父进程 |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | `128000`，CC 环境变量接受的值 |
| `CLAUDE_CODE_EXTRA_BODY` | `max_tokens=131072`，`output_config.effort` 对应 high/max |
| Node 启动 | `node --require /opt/cc-node-runtime/timeout.cjs /opt/claude-code-2.1.110/package/cli.js` |
| 桥接与 CDP | 本机 :8083、:9225，5 个标签；记录实际新端口及源码身份 |
| 外网代理 | 需要时使用 `http://127.0.0.1:31128`；本机 bridge/CDP 直连 |

新计划初始化后，仍需按原生说明补齐正式协议与 `bridge_runtime_by_stage`；`init` 本身不会自动填好这些运行时配置。启动正式阶段的命令为：

```sh
sudo -n python3 tools/eval_terminal_native.py run --base logs/<new-run> --stage formal-128k-fixed --budget 28800 --repeats 3
```

## 继续报告时保留的判断边界

CC 在 taomni 的有监督交付已通过三平台 browser/native 75/75：分支 `feat/cc-bridge-notes-search-20261109`，提交 `591ce56ff55fa24794c392aaa18047c4e6766997`，GitHub run [42659287606](https://github.com/engcapa/taomni/actions/runs/42659287606)。该成果不需要重做，但不能称为初稿全对或无人值守成功。

基准中既有正常结束但修复不完整的反例，也有产物正确却被传输错误打断的反例。继续独立验收产物，同时记录客户端完成状态和桥接传输。固定 128K 表示允许的输出上限，尚未证明单次实际生成 128K；真实 HTTP/2 故障和最初 taomni 主 agent 中断的根因尚未闭环。当前五题原生适配结果不能转换为 Artificial Analysis 官方智能指数，也不足以判定 high/max 的稳定优劣。
