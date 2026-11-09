# Claude Code 桥接服务实测报告（2026-11-09 至 2026-11-10）

本报告记录通过 `nvplay-bridge` 使用 Claude Code（下文简称 CC）分析、修改和验证 taomni 的全过程。实测时间为 2026 年 11 月 9 日晚至 11 月 10 日凌晨，所有时间均为北京时间（Asia/Shanghai，UTC+08:00），最终统计截止于 11 月 10 日 02:13:16。

本次中文整理依据已保存的日志和原始测试产物完成，没有重新执行测试。下文的进程、标签和服务状态均对应实测时的快照。原始日志、会话、设置和下载产物保存在本地被 Git 忽略的 `logs/` 中，没有随报告提交，也不属于可移植的测试夹具。

## 一、实测结论

本轮完成了原手工 CC 会话跟进、taomni 缺陷修复及新功能开发、独立代码审查、三平台 browser/native 执行，以及桥接服务修复和实际部署。

| 验证对象 | 实测结果 |
| --- | --- |
| 原手工 CC 主会话 | 发生过真实读流错误；继续后于 11 月 10 日 00:28:59 完成工程分析，未发现传输死锁证据 |
| taomni 产品变更 | 修复 Notes 筛选状态异步保存乱序；新增搜索清空按钮和 Escape 操作；在新分支提交并推送 |
| GitHub UI 自动化 | 所选 Notes 和全部适用的 `crash-regression` 用例共 75/75 执行单元通过，UI 用例无失败、无跳过 |
| 新增用例 `TC-NOTES-014` | Linux、Windows、macOS 的 browser/native 六个组合均通过 61/61 步及 R1/R2/R3 验证要求 |
| 桥接离线回归 | 最终 139 项通过 |
| 真实 Brave/CDP 故障注入 | 两种 HTTP 错误 × 两套 API 协议，共 4 个场景通过 |
| 生产桥内重试 | 一次真实 NVIDIA 请求连续两次 HTTP 500 后，第三次尝试成功，CC 未收到中断错误 |
| 仍未解决的问题 | 已交付内容后的 `TypeError: network error` 曾再次发生，底层网络或上游原因尚未确定 |

75 个执行单元是本次选定用例在不同系统和运行模式下的执行次数，不代表全仓库 714 个用例全部通过，也不代表完整 Cargo 测试或物理操作系统输入行为均已验证。

## 二、环境与工作范围

| 项目 | 配置 |
| --- | --- |
| 桥接工程 | `/home/zhyhang/code/person/nvplay-bridge` |
| 被测产品 | `/home/zhyhang/code/person/taomni` |
| 原始启动方式 | `start-bridge.sh` |
| 生产服务 | `127.0.0.1:8083` |
| 浏览器连接 | Brave，CDP 端口 `9225`，5 个 `kpool` 标签 |
| CC 版本 | `2.1.319` |
| 上游模型 | `moonshotai/kimi-k3.1-pro` |
| taomni 分支 | `feat/cc-bridge-notes-search-20261109` |
| taomni 提交 | `591ce56ff55fa24794c392aaa18047c4e6766997` |
| GitHub workflow | `qa-ui-auto-platforms.yml` |
| GitHub 运行 | [42659287606](https://github.com/engcapa/taomni/actions/runs/42659287606)，attempt 1 |

taomni 变更由 CC 在新分支完成；监督者独立检查复现结果、代码、测试断言和最终产物。桥接修复按要求直接提交到 `main`，提交说明使用详细英文。实测中使用过独立的 `8084` canary 服务和 `kcanary` 标签，以验证修复并隔离生产标签。

## 三、原手工会话的中断调查

### 关键时间线

| 时间 | 观察与判断 |
| --- | --- |
| 11 月 9 日 22:57:01 | 请求 `cfea693e`、`ff544a86` 返回 `upstream 500: None`，此前均未交付上游内容 |
| 23:02:24–25 | 请求 `929e8ff1`、`0a95872f`、`8ea44fc7` 同时段出现 `TypeError: network error`，此前均已交付内容 |
| 23:02:24.891 | 主会话记录 `Server error mid-response`，与用户观察到的中断一致 |
| 23:03:13 | 用户输入“继续”；后续日志时间戳、生成 token 数和工具结果证实主会话及后台 agent 仍有进展 |
| 23:53:46 | 原两个后台分析 agent 的会话记录显示被中断 |
| 11 月 10 日 00:28:59 | 原主会话完成工程架构分析 |

后续一条 max effort 请求持续生成推理约 713 秒，最终返回 3 个 Read 调用；再下一条请求的输入达到 192,558 tokens。持续返回的数据和最终工具调用表明，这段等待包含长时间模型推理，不能仅凭界面停留认定桥接死锁。

同时发生的三次异常属于已观察到的上游读流失败，其底层网络或提供方根因没有查明。最初两次 `500: None` 的错误正文已经丢失，无法事后证明它们就是 NVIDIA 连接池超时。

对原架构报告的独立抽查确认了数据库启动 `expect` 路径、release finalizer 依赖、终端/SFTP 保留挂载、应用版本和双 lockfile 等具体描述。数据库故障隔离和长期保留运行时属于静态风险，没有复现数据库损坏或资源泄漏。release workflow 缺少直接 QA 依赖，也不能单独证明不存在外部发布策略。完整代码行数和 IPC 命令数量未重新统计，没有据此开展大范围产品修改。

## 四、确认的桥接缺陷与修复

### 1. HTTP 错误正文被过早丢弃

浏览器 `fetch()` 可以先返回 HTTP 状态，响应体 reader 随后才收到正文。原 CDP 循环遇到非 200 状态立即抛错并中止读取，导致“错误响应头先到、正文分块后到”的场景退化为 `500: None`。这既损失诊断信息，也使现有 NVIDIA 连接池超时重试无法识别错误特征。

`tests/test_browser_http_errors.py` 先在旧实现上复现了正文丢失。修复后最多收集 8192 个字符、等待最多 5 秒，同时遵守取消信号和原始请求总时限；HTTP 错误正文不会作为成功模型输出发送。受限长度的正文能够跨线程与异步边界保留，正文前缀较长时仍可识别后面的连接池超时标记。空正文、超长、停滞和读取失败均有明确诊断。

同时修复了诊断计数：原 `tool_calls` 将每个流式参数片段都算成一次调用，现按不同的上游调用索引计数。失败日志记录 HTTP 状态、是否已交付内容、计数和 usage，以区分可重试失败与流中途失败。

该阶段 9 项新增错误边界测试通过，显式离线回归共 129 项通过。后续修复完成后的最终总数为 139 项。

### 2. 对未交付内容的明确 HTTP 5xx 增加重试

23:46:34 的实网请求收到明确 HTTP 500，但 5 秒窗口内没有取得正文。CC 随后携带相同历史改用非流式请求重试。这说明仅依赖特定 NVIDIA 错误正文的策略无法覆盖全部预交付失败。

新增策略保留真实 HTTP 状态来源，在以下边界内重试：

- NVIDIA HTTP 500 且正文包含 `Pool#acquire` 超时特征：保留专项策略，默认额外尝试 2 次，首次等待 5–6 秒，第二次等待 10–11 秒。
- 其他明确收到的 HTTP 500/502/503/504：默认额外尝试 1 次，等待 2–2.4 秒。专项策略优先，预算独立，专项次数用尽后不会借用通用预算。
- 只有尚未向下游交付 thinking、正文或工具调用时才可重试；已交付内容后明确返回错误，避免拼接两次答案或重复工具调用。
- 重试保持同一 SSE 连接、请求体、effort、历史、工具及原始总时限；退避期间继续心跳，并响应客户端断开和取消。
- 未建立 HTTP 响应的网络失败、HTTP 200 流内的应用错误以及读流异常，不会被误判为已收到的 HTTP 5xx。

该逻辑覆盖 browser 模式下的 Anthropic Messages 和 OpenAI Responses，包含流式、非流式接口。详细配置见[浏览器上游 HTTP 错误重试](upstream-pool-timeout-retry.md)。

### 3. 实网探针在导入时启动网络工作

两个测试脚本原本在模块导入阶段发起实网请求。首次测试发现触发该行为后已停止，并关闭独立 `ktest` 页面。脚本改为显式 `main` 入口，离线测试发现不再导入并启动实网工作。

`tests/probe_browser_http_errors.py` 是显式启用的真实 Brave/CDP 探针。隔离的本地页面先返回 HTTP 500 响应头，再延迟返回正文分块，下一次请求返回成功 SSE。最终覆盖专项连接池错误、通用服务器错误与 Messages、Responses 的四种组合；每种均恰好请求两次后成功，没有发送 NVIDIA 推理请求或操作生产标签。

### 4. 监控遗漏半条日志和长期请求

监控原来会丢弃尚未写完的 JSON 行，不能正确处理日志截断或替换。修复后按字节偏移跟踪，保留不完整 UTF-8/JSON 记录，识别文件身份变化，并输出标签池状态、请求 ID、持续时间、最近活动、计数、usage 和近期错误。3 项针对性测试覆盖分段写入、截断/替换及重试/失败跟踪。

长任务又暴露一个缺陷：先按 `--lookback` 过滤日志，再重建请求状态，会丢失窗口之前开始且仍活跃的请求。实测出现“池内有 busy 标签，active 列表却为空”。现改为先使用完整可用日志重建请求生命周期，仅对事件总数和近期错误应用窗口；新增回归覆盖持续返回和暂时无输出的旧请求，也确保不会复活已完成请求或旧错误。

## 五、CC 完成的 taomni 工作及独立审查

### 缺陷：Notes 筛选状态保存乱序

Notes 工作区的筛选偏好通过异步原生接口保存。数据库 mutex 能串行化访问，但不能保证多个 JavaScript IPC 请求按调用顺序取得锁。

监督者在原实现上独立复现：界面已经选择 `completed`，较早但延迟完成的 `all` 写入仍可覆盖最新状态。CC 加入每个 store 独立的 Promise 队列后，同一竞态回归通过。队列在写入失败后继续处理后续更新，抑制过期版本的错误，同时保留最新版本的错误。

### 新功能：清空 Notes 工作区搜索

新增本地化、可访问的清空按钮和 Escape 操作：

- 清空查询后恢复输入框焦点，保留状态/标签筛选、当前选中笔记和未保存草稿。
- 非空搜索的 Escape 被处理；空搜索的 Escape 继续向父级冒泡。
- 对 IME 的 `isComposing` 和 `keyCode === 229` 加保护。
- 仅对工作区搜索输入框抑制重复的浏览器原生清空装饰。

### 审查中要求修正的问题

| 问题 | 独立检查与修正 |
| --- | --- |
| 初稿测试夹具或 spy 没有真实驱动受控输入 | 要求输入确实更新、回调确实调用，才接受断言结果 |
| IME 和 Escape 测试不完整 | 补充真实 Escape 事件、两种 IME 保护及父级冒泡的可观察断言 |
| 失败写入后的持久化预期不正确 | 按实际成功/失败顺序纠正期望，验证失败后队列仍可前进 |
| 将 WebKit 和 MS 专有伪元素合并为一个选择器列表 | 真实 CDP/CSSOM 复现 Brave 丢弃整条规则；拆分后 WebKit 规则被保留 |
| 新用例同时保留默认状态又开启 Pinned，却期望仅按 Pinned 筛选 | 明确先切换为 `all` 再启用 Pinned，并纠正步骤引用和验证编号 |

初稿缺失断言的结果和仅经过审阅的计划均未作为执行通过证据。

产品最终提交为 `591ce56ff55fa24794c392aaa18047c4e6766997`，分支为 `feat/cc-bridge-notes-search-20261109`，由 CC 推送。共 13 个文件，包含实现、测试、用例、QA 策略/依赖、翻译及生成的控件目录。提交标题为 `fix(notes): serialize filter writes and clear workspace searches`。

## 六、验证结果与证据强度

| 检查 | 结果及范围 |
| --- | --- |
| 桥接最终离线测试 | 139 项通过，耗时 16.412 秒 |
| 真实 Brave/CDP 探针 | 4 个场景通过，每个场景两次请求后成功 |
| CC Bash + Read 烟测 | 通过修复后的 canary，3 轮约 133 秒，返回的 SHA-256 和 README 标题经独立核对 |
| 取消与继续 | 23:27:11 主动中断后约 55 毫秒确认浏览器 abort 和 `browser_cancelled`；同一 CC 会话恢复执行 |
| 更新后生产服务烟测 | Bash 返回正确的 `tools/monitor_bridge.py` 摘要和 `BRIDGE_MAIN_OK`；2 轮约 12.7 秒，退出码 0 |
| Notes 针对性测试 | 6 项 store 测试、5 项挂载组件测试，共 11 项通过 |
| Notes 保留行为测试 | 独立使用 Node 22/pnpm 10 执行，24 项通过 |
| 产品静态与合同检查 | TypeScript、`check:refactor`、控件目录生成、QA audit、提交后 development case contract 均通过 |
| 独立本地 browser 用例 | Brave 157 实际执行 `TC-NOTES-014`，61/61 步、R1/R2/R3 通过，并检查截图 |
| CI 完整 Vitest | 703 个测试文件通过，6404 项通过、1 项跳过 |
| CI 原生合同检查 | 129 项相关前端/mobile 测试及 97 项 release Rust 测试通过；另有 1981 项 Rust 测试被筛除 |
| CI UI 执行 | 三平台、两种模式，共 75/75 通过，无 UI 用例失败或跳过 |

Vitest 的 1 项跳过来自 TerminalPanel 已有的互斥 text-mirror 变体，测试和跳过条件均未在本次修改。97 项 Rust 结果属于选中能力的合同测试，不代表完整 Cargo 回归。

本地 Brave 通过显式包装器启动，记录了实际可执行文件身份，没有修改仓库 runner；该结果是补充浏览器证据。原生平台结果分别由对应系统的实际 QA release 应用提供。

### 测试与监督脚本中的失误

实测中也记录了与产品或桥接故障不同的设置、脚本错误，并在验收前纠正：

- 监督者一次命令误用了系统 pnpm，触发依赖重装并导致 CC 随后出现 `vitest not found`。已停止重装，使用 Node 22/pnpm 10 冻结依赖离线恢复，耗时 3.7 秒，lockfile 未改变。
- 一次目录生成命令使用 `python -I`，导致 `PYTHONPATH` 不生效；修正后重新完成 audit。
- 一份监督设置虽使用 `low` CLI 参数，`CLAUDE_CODE_EXTRA_BODY` 中却仍显式指定 `medium`。请求日志证实桥接保留了该值。修正设置后，新的短上下文会话两次请求实际均为 `low`，24.0 秒内完成 Bash 分支/提交检查和回答。
- CC 的临时核验脚本曾混淆路径参数、规范化选择摘要与文件字节摘要；最终又将每组的 `selected`/`counts` 误作汇总顶层字段，触发 `KeyError`。均在最终结论前修正并重跑。

上述失败日志单独保留，没有作为产品回归失败或桥接传输失败计数。`low` 与 `medium` 会话曾同时使用不同生产标签，未出现工具结果混串；这支持本次并发隔离观察，不能作为严格的 effort 性能对比。

## 七、GitHub 三平台 browser/native 实测

CC 于 11 月 10 日 00:53:35 推送分支并调度 [run 42659287606](https://github.com/engcapa/taomni/actions/runs/42659287606)。实际测试提交与审查提交完全一致。02:07 观察到整个 workflow 为 `completed/success`。

### 输入与选中范围

```text
scope=selected
head=591ce56ff55fa24794c392aaa18047c4e6766997
platforms=linux,windows,macos
modes=browser,native
case_ids=TC-NOTES-014,TC-NOTES-013,TC-NOTES-012
tags=crash-regression
native_release=true
publish_issues=false
```

独立比对下载的 `selection.json` 与本地计算的预期集合，六组均无遗漏或额外用例。每个平台 browser 12 个、native 13 个；每个 native 组拆为 4 个分片并共享一个 release 构建，共 15 个执行 entry。

`run_rdp_unit_contracts` 保留默认 `true`，但本次用例集合没有选中 RDP contracts。实际选中的原生合同能力是 `mobile`。Tool、SFTP、development contract 等未选中的 job 被跳过；development contract 已在本地对产品提交独立验证通过。问题发布关闭，没有创建 issue。

### 实际执行结果

| 平台 | Browser | Native release | 新用例在每种模式下的结果 |
| --- | ---: | ---: | --- |
| Linux | 12/12 | 13/13 | 61/61 步，R1/R2/R3 全部通过 |
| Windows | 12/12 | 13/13 | 61/61 步，R1/R2/R3 全部通过 |
| macOS | 12/12 | 13/13 | 61/61 步，R1/R2/R3 全部通过 |
| 合计 | 36/36 | 39/39 | 六个组合全部通过 |

75 个执行单元中，Notes 为 18 个，适用的完整 `crash-regression` 为 57 个。没有失败或跳过的 UI 用例，也没有遗漏的执行 entry。

监督者独立检查了六个组合的最终截图：搜索框为空且保持焦点，选中笔记及筛选结果正确，未保存草稿保留。最终截图不包含非空搜索时的清空按钮，因此不能据此确认各 WebView 中该按钮与原生装饰的视觉间距。

### 原生构建身份

三个平台均使用隔离的 `com.taomni.app.qa`、release profile，记录了隔离数据/配置/缓存路径。同一平台的四个分片使用相同二进制，其摘要与各自 build/summary 身份一致，源码指纹与选择清单一致。

| 原生构建 | 二进制 SHA-256 |
| --- | --- |
| Linux x64 release | `e87433534eca3f331dcc5351fab712964c36293d18d5052311a6295820343894` |
| Windows x64 release | `247ad81e303914e7a42c328afc71e1321047ccdf3efda46fe26db9303cb16dbe` |
| macOS ARM64 release | `04f46a4cc2ae26338791c7d1c13e2f8540adb8d451220b9c5a43b8b56749e5be` |

Linux 的 Ubuntu 22.04 构建产物实际运行在选定的 Ubuntu 24.04 Xvfb/Openbox 桌面，readiness 和桌面元数据均核对通过。Windows 使用实际 WebView2 适配器，macOS 使用实际 Darwin ARM64/WKWebView。后两者及 Linux 的执行报告均经过原始产物核验。

### 独立产物复核

监督者逐项检查了真实状态、精确用例集合、用例摘要、稳定源码/runner 身份，以及非 dry-run 标记；并检查 receipt 所列文件实际存在、实际 SHA-256 匹配、receipt 绑定 summary、outcome 绑定 head/run/attempt/规范化选择摘要、退出码为 0。读取 Windows 路径时仅将反斜杠标准化，未修改原始报告。

从 15 份原始报告重新运行 `qa_ui_auto.ci.aggregate`，与下载的 `ci-summary.json` 完全一致，仅后者额外包含 `job_results`。最终下载的选择清单也与原始计划相等。

| 汇总字段 | 最终值 |
| --- | --- |
| `passed` / `report_ok` | `true` / `true` |
| `failures` | `[]` |
| `infrastructure_errors` | `[]` |
| `gaps` | `[]` |
| `unreviewed` | `[]` |
| 独立复核的 `pending` / `errors` | `[]` / `[]` |

最终 CC 会话复用已审查的原始报告检查脚本，独立抽查 Windows 四个分片的构建身份，并通过 GitHub API 查询实际运行状态。最终结论与监督者一致，02:12:33 完成回答，关闭 stdin 后退出码为 0。

| 身份 | 摘要 |
| --- | --- |
| 规范化选择摘要 | `882cebc91e09309d4d1f9fe7a5c3b2362bb3a50d3151f64ffb576dc54cdf307b` |
| 源码指纹 | `2deda7807ab6e064916be68f9760596e962c04c44e1bfe2876eafb3b12124420` |
| Runner 指纹 | `50b8b5a550a9d7c082ab5d55b4651f5998e7828ffe3dbf8809876e380a0d128d` |
| `TC-NOTES-014` 文件摘要 | `f2ba370b547317b568b3fc5574aeb589d1a6410084db43202a7384fd7f62ff4f` |

规范化选择摘要不同于 `selection.json` 的原始文件字节摘要。所有组合均绑定同一完整产品提交、run 42659287606 和 attempt 1。

### Receipt 信任边界

当前 receipt 验证只能证明仓库现有格式的一致性，不能作为不可伪造的密码学证明。独立阅读 `runner_receipt.py` 发现，默认 key 记录公开，`compute_receipt_signature` 使用由 32 位 FNV 派生并重复的校验值，与其密码学描述不符。

因此，本报告还核对了实际 GitHub job 执行、精确提交、原始产物、真实 SHA-256 和用例结果，没有仅凭 receipt 校验通过就认定执行可信。对恶意报告作者的防伪认证未得到证明；迁移产品 receipt 协议未纳入本轮 Notes/桥接修改。

## 八、生产更新与真实故障恢复

11 月 10 日 00:20:15，先在空闲 API 边界更新独立 canary 到 `2c2674d`，后续监督 CC 请求正常。

00:35:37，在确认生产服务空闲、五个标签全部 ready 后，将 8083 服务由 PID 26861 平滑替换为 PID 161859，加载 `bac48c184e2ed5f75220a76666b7db66811284c3`。保留原环境、uvicorn 参数、CDP 9225 和五个 `kpool` 标签。原手工 CC 在空闲边界短暂停住后恢复，没有因更新终止在途请求。

### 真实 NVIDIA 连接池超时在桥内恢复

请求 `c25fb817` 的前两次尝试均收到 HTTP 500，取得完整的 330 字符 `Pool#acquire` 错误正文，每次约等待 49 秒。桥接正确识别专项连接池超时，确认浏览器 abort，释放并恢复失败标签，分别退避 5.712 秒和 10.290 秒。

第三次尝试使用健康标签，在总耗时 142.795 秒后返回一次工具调用。失败尝试没有交付 thinking、正文或工具内容，CC 没有收到 API 中断错误，随后完成分支推送和 workflow 调度。这是实际 NVIDIA 上游恢复证据，补充了四项可控 CDP 注入结果；它不能证明持续拥堵时必然成功。

### 已交付内容后的网络错误仍然存在

01:06:11，请求 `52bf8541` 在运行 81.071 秒、已交付 8117 个 thinking 字符和 85 个正文字符后，遇到 `TypeError: network error`。

桥接记录 `committed=true`，明确返回错误并恢复失败标签，没有进入 HTTP 重试或重放已有内容。CC 在后续排队指令下继续，请求 `93aa1eb8` 于 74.687 秒后成功返回工具调用。

该复发说明修复改善了预交付 HTTP 错误恢复，但没有消除流中途读流异常。其底层网络或上游原因仍未确定。

### 最终生产观察窗口

统计区间为 00:35:37 生产更新后至 02:13:16：

| 指标 | 数量 |
| --- | ---: |
| 逻辑浏览器请求 | 58 |
| 正常完成 | 57 |
| 流中途终态失败 | 1 |
| 桥内 HTTP 重试 | 2 |
| 上游尝试总数 | 60 |
| 取消 | 0 |
| 最终活跃请求 | 0 |
| 标签池恢复错误 | 0 |

最终快照显示服务健康，五个生产标签均 ready。该统计只描述本次工作负载，不构成可靠性概率估计。前述主动取消发生在生产更新之前，不包含在这个窗口内。

监督新增的实现、workflow、最终复核会话均已结束；canary 服务及页面、本地 Vite、GitHub 监控已停止或正常结束。截止该快照，8083 生产服务、Brave/CDP 9225 和原手工 CC 进程保留运行。

## 九、提交与本地证据索引

桥接修复直接在 `main` 提交，均使用详细英文说明：

| 提交 | 内容 |
| --- | --- |
| `dba70af` | 保留有界的延迟 HTTP 错误正文，纠正工具调用计数 |
| `30987a2` | 隔离显式实网探针，修复半条日志、截断与替换场景 |
| `2c2674d` | 对未交付内容的明确 HTTP 5xx 重试，保留流语义和总时限 |
| `bac48c1` | 修复监控时间窗口遗漏长期活跃请求 |
| `3c5b1f9` | 记录最终实测结果、生产更新、GitHub 证据及验证边界 |

上述提交的代码测试结果在本次中文整理前已完成。当前中文变更仅修改报告，未重跑产品构建或测试；提交是否已推送应以实际 Git 状态为准，不从历史快照推断。

| 本地证据路径（相对桥接工程） | 用途 |
| --- | --- |
| `logs/offline-discovery-final.log` | 最终 139 项离线回归 |
| `logs/cdp-error-probe.log` | 四项真实 Brave/CDP 探针 |
| `logs/notes-review-red.log` | 原实现的乱序写入复现 |
| `logs/notes-review-race-green.log` | 修复后的竞态回归 |
| `logs/notes-review-retained-node22.log` | 24 项 Notes 保留行为检查 |
| `logs/notes-review-css-parser.json` | CSSOM 选择器问题复现 |
| `logs/notes-review-dev-contract.log` | 独立 development case contract 检查 |
| `logs/ci-42659287606/reports/` | 15 份原始执行 entry 报告及产物 |
| `logs/ci-42659287606/aggregate/ci-summary.json` | GitHub 最终汇总 |
| `logs/ci-42659287606/independent-review.json` | 逐 entry 独立核验 |
| `logs/ci-42659287606/independent-aggregate.json` | 从原始报告重算的汇总 |
| `logs/bridge-final-metrics.json` | 生产更新后的最终请求统计 |
| `logs/main-restart-state.json` | 生产进程更新记录 |
| `logs/cc-final.jsonl`、`logs/cc-final-exit.json` | CC 最终复核及退出结果 |

## 十、仍需保留的限制与后续方向

1. **流中途网络错误尚未根治。** 需要进一步定位浏览器 reader、网络和上游提供方的相关证据；不能通过无条件重放已交付内容规避，否则可能重复答案或工具调用。
2. **物理 OS 输入尚未认证。** 合成 IME 保护和 WebView 自动化通过，不代表物理输入法、全局快捷键、AppKit 菜单栏点击或原生窗口输入链路已全部验证。
3. **视觉证据有明确范围。** 最终截图证明清空后的焦点、筛选和草稿状态，没有证明非空清空按钮在所有 WebView 的装饰间距。
4. **Receipt 不具备已证明的密码学防伪性。** 本次以实际 GitHub 执行、原始文件及 SHA-256 交叉核验；协议本身仍需单独设计和迁移。
5. **覆盖限于本轮选中范围。** 75 个 UI 执行单元、相关合同和已列回归均通过；未执行全仓库 714 用例、完整 Cargo 测试或本次未选中的 RDP contracts。
