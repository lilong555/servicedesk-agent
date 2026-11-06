# 工单级确认偏好实验结果

## 结论

固定六个合成开发场景、每场景两条件和两次独立重复，正式组完成 **24/24 次、12 个配对**。指标为**固定用户补充策略下的精确匹配成功率**：真实记忆可召回组 available **10/12**，约定空结果组 empty **12/12**；available 少 **8 轮偏好补充**，同时精确匹配成功少 **2/12，下降 16.67 个百分点**。两次失败均为 `log_summary` 的标点保留失败，原分数和业务写入完整保留。

固定偏好补充包含完整期望备注，empty 的八次历史偏好执行都得到这种用户帮助；available 从已召回格式与当前事实自行组装。结果衡量包含后续用户帮助的完整流程。全部用户补充为 **available 0 / empty 9 轮**，其中偏好补充 **0 / 8 轮**；调用和输入下降与精确匹配成功率同时呈现。

按每条件全部 12 次执行计，available 相对 empty 的 Agent 请求减少 **55/138 = 39.86%**，逻辑输入减少 **299,977/651,680 = 46.03%**。这些总量包含两次失败的全部请求。正式组没有重跑、替换题目或剔除失败。三轮 pilot 的 12 次执行单独计账。

数据入口：[独立目录正式复算](../../results/runs/preference-memory-20261106-audit-public/formal.json)、[原始汇总](../../results/runs/preference-memory-20261106-formal/summary.json)、[归档校验与总账](../../results/runs/preference-memory-20261106-verification/audit.json)。

## 配置与分母

真实组件为生产 `execute_session`、DeepAgents、LangMem、SqliteStore 和 `ModelSettings` 构建的 `gpt-6-luna`。模型配置：temperature=0、max_retries=0、parallel_tool_calls=false、timeout=60 秒、max_tokens=8192；每执行最多 3 个用户轮次、36 个模型请求，recursion_limit=48。业务后端采用现有 `RedmineFixture`，通过真实 loopback HTTP 提供合成 Redmine 服务。固定用户脚本产生补充答复，模型模拟器请求为 0。

六个独立场景包括四个历史偏好场景和两个当前指令覆盖场景；每个场景重复两次，两条件按 AB/BA 交错，第二次重复反向。执行串行，各次执行重置业务状态并使用独立 Store。相同 HTTP 服务地址维持相同 service/project/API 身份/工单 scope；每次建立阶段经生产 `remember_confirmed_preference` 显式写入确认文本，关闭 Store，再在新 thread 重开。建立阶段模型请求为 0。

两条件共享 Schema、Skills、政策、权限与新会话输入，仅确认偏好查询入口返回真实结果或 `[]`；两者都实际调用原生 LangMem 查询。两条件共同提供 `filter=null` 参数说明。新会话首请求不含旧消息、交接内容、历史 journal、用户约束或答案标记。available 12/12 次执行均至少一次真实召回旧偏好；empty 12/12 次执行实际搜索，返回空结果。

固定评分含五项布尔断言：一次 HTTP 写入、正文精确匹配、一个 journal、其他字段保持原值、有效回读回执。正文仅规范化空白并移除生产操作标记，标点参与评分。正式组共 **120 项断言，118 项通过**；严格任务成功要求五项全部通过，共 **22/24**。独立场景数为 6，配对数为 12，执行数为 24，断言数为 120。

依赖版本：DeepAgents 0.7.32、LangMem 0.0.30、LangGraph 1.2.20、langgraph-checkpoint-sqlite 3.1.3、langchain-openai 1.7.3。完整参数、依赖、调度与源码哈希见 [freeze-v3](../../results/runs/preference-memory-20261106-freeze-v3/freeze.json)。

## 正式逐对结果

表内均按 available / empty 排列；成功配对为 available 胜 0、负 2、平 10，分母 12 对。

| 场景 | 重复 | 固定补充策略下精确匹配 | 偏好补充轮数 | 模型请求 | 逻辑输入 token |
| --- | --- | --- | --- | --- | --- |
| note_reference | 1 | 成功 / 成功 | 0 / 1 | 7 / 24 | 28,686 / 147,869 |
| note_reference | 2 | 成功 / 成功 | 0 / 1 | 6 / 11 | 24,723 / 48,908 |
| log_summary | 1 | 失败 / 成功 | 0 / 1 | 7 / 12 | 29,350 / 52,939 |
| log_summary | 2 | 失败 / 成功 | 0 / 1 | 7 / 14 | 29,433 / 68,657 |
| contact_route | 1 | 成功 / 成功 | 0 / 1 | 8 / 12 | 33,798 / 52,977 |
| contact_route | 2 | 成功 / 成功 | 0 / 1 | 9 / 12 | 41,887 / 54,532 |
| audit_timestamp | 1 | 成功 / 成功 | 0 / 1 | 6 / 13 | 24,912 / 58,679 |
| audit_timestamp | 2 | 成功 / 成功 | 0 / 1 | 8 / 11 | 33,878 / 49,243 |
| override_reference | 1 | 成功 / 成功 | 0 / 0 | 6 / 8 | 25,344 / 32,756 |
| override_reference | 2 | 成功 / 成功 | 0 / 0 | 7 / 7 | 29,399 / 28,346 |
| override_contact | 1 | 成功 / 成功 | 0 / 0 | 6 / 7 | 25,415 / 28,352 |
| override_contact | 2 | 成功 / 成功 | 0 / 0 | 6 / 7 | 24,878 / 28,422 |

历史偏好子集每条件 8 次：available 成功 6/8、empty 8/8；偏好补充 0 对 8 轮，请求 58 对 109 次。当前指令覆盖子集每条件 4 次：两组均成功 4/4，偏好补充均为 0，请求 25 对 29 次；available 的 4 次执行均实际召回旧偏好，再按当前指令写入。

全部用户轮次为 available 12、empty 21。扣除各执行首轮请求后，**全部用户补充为 0 / 9 轮，偏好补充为 0 / 8 轮**。empty 的额外 9 轮包括 8 轮偏好补充与 `note_reference/r1` 的 1 轮普通任务跟进。补充次数按真实已发送用户消息核算，首次业务写入后停止补充。

## 两次失败证据

历史确认文本要求一行 `SIGNAL=<finding>; FOLLOWUP=<next action>`，并保留所给 finding 与 action 文本。首个新指令给出 `Finding: Packet loss is 2%. Next action: Collect two hours of traces.`。冻结期望与实际正文如下：

```text
期望: SIGNAL=Packet loss is 2%.; FOLLOWUP=Collect two hours of traces.
r1:   SIGNAL=Packet loss is 2%; FOLLOWUP=Collect two hours of traces.
r2:   SIGNAL=Packet loss is 2%; FOLLOWUP=Collect two hours of traces
```

r1 的查询为 `log summary format`，r2 为 `format log summary ticket 42`，均使用 `filter=null`，返回一条真实、完整的历史确认记录。随后模型实际写入上述正文，服务端各形成 journal #1，生产回读回执均为 verified。两次各有 7 个模型请求、0 次偏好澄清、0 个基础设施异常。四项业务断言通过，`note_matches=false`。证据支持“召回成功后未完整保留输入标点”的错误定位；严格失败保持冻结评分。

| 重复 | 召回原文 | HTTP 写入 | 服务端 journal | 评分与回执 |
| --- | --- | --- | --- | --- |
| r1 | [memory_searches](../../results/runs/preference-memory-20261106-formal/04-log_summary-available-r1/memory_searches.json) | [writes](../../results/runs/preference-memory-20261106-formal/04-log_summary-available-r1/writes.json) | [after](../../results/runs/preference-memory-20261106-formal/04-log_summary-available-r1/after.json) | [result](../../results/runs/preference-memory-20261106-formal/04-log_summary-available-r1/result.json) |
| r2 | [memory_searches](../../results/runs/preference-memory-20261106-formal/15-log_summary-available-r2/memory_searches.json) | [writes](../../results/runs/preference-memory-20261106-formal/15-log_summary-available-r2/writes.json) | [after](../../results/runs/preference-memory-20261106-formal/15-log_summary-available-r2/after.json) | [result](../../results/runs/preference-memory-20261106-formal/15-log_summary-available-r2/result.json) |

空结果条件收到的固定澄清答复包含完整组装后的目标备注；available 从格式偏好和当前事实自行组装。这一输入差异属于冻结多轮协议，解释成功率时应与确认次数同时呈现。样本是六题合成开发集，结果描述本配置下的行为和代价。

## 请求与用量

全部为原始 UsageRecorder 的 `agent` 角色请求，包括失败任务、模型内部额外调用和用户补充后的请求。建立阶段、LangMem 查询、固定用户脚本与 HTTP fixture 的额外模型请求均为 0，所以 Agent-only 与含模拟器/建立阶段的模型总量一致。逻辑输入包含 cache read tokens，缓存列为输入的子集；三列分别统计。价格参数为空，所有费用保持 `null`。

| 正式口径 | available，12次 | empty，12次 | 合计，24次 |
| --- | ---: | ---: | ---: |
| 模型请求 | 83 | 138 | 221 |
| 逻辑 input tokens | 351,703 | 651,680 | 1,003,383 |
| output tokens | 3,576 | 6,398 | 9,974 |
| cache read tokens | 260,736 | 499,712 | 760,448 |
| 失败模型请求 | 0/83 | 0/138 | 0/221 |
| 未知用量请求 | 0/83 | 0/138 | 0/221 |
| 基础设施异常 | 0/12 | 0/12 | 0/24 |
| 全部用户补充轮数 | 0 | 9 | 9 |
| 其中偏好补充轮数 | 0 | 8 | 8 |
| HTTP 备注写入 / journal | 12 / 12 | 12 / 12 | 24 / 24 |
| 偏好查询 / 非空返回 | 13 / 12 | 14 / 0 | 27 / 12 |

模型 callback 耗时累计 available 266.951 秒 / 83 请求，empty 465.786 秒 / 138 请求；逐执行墙钟累计 269.913 秒 / 12 次和 468.976 秒 / 12 次，另有建立阶段 1.789 秒和 1.755 秒。全部请求都有耗时记录。串行 AB/BA、相同模型和后端支持本轮描述性比较；CPA 共享负载、网络与服务端提示缓存保持实际状态，延迟仅报告观测值。

## 三轮 Pilot 与冻结

三轮开发 pilot 均使用 `note_reference` 和 `override_reference` 两场景，各执行 available/empty 一次，每轮 4 次，合计 **12 次执行、112 次请求**。正式六场景复用了这两个 pilot 场景，另含四个开发场景。六场景与业务评分始终保留；源码归档记录各轮工具说明和 gate 的配置，正式结果包含第三轮添加的两条件共同工具指导。

| 轮次 | 成功 | 请求 | Gate | 具体问题 |
| --- | ---: | ---: | --- | --- |
| pilot / freeze | 3/4 | 46 | false | `override_reference/empty` 连续把 64 位 source_version 抄成 63 位，生产校验拒写；4 次 add_ticket_note 尝试均在 HTTP 写入前被拒绝。原 gate 要求四次全部业务成功，普通可评分失败阻断放行。部分召回还错误使用顶层 ticket_id filter。 |
| pilot-v2 / freeze-v2 | 4/4 | 36 | false | Gate 从全部业务成功改为完整评分、每条件至少一次成功，并要求真实非空/空分支。available 的历史偏好查询使用 `filter={"ticket_id":42}`，存储顶层没有该字段，返回空；另一个 available 执行未搜索。非空召回覆盖为 0/2，未验证完整接线。 |
| pilot-v3 / freeze-v3 | 4/4 | 30 | true | 两条件共同加入准确的 `filter=null` 参数说明；available 2/2 执行实际非空召回，empty 分支和首输入无泄漏检查通过。正式采用此冻结。 |

formal 的 `pilot_gate_passed=false` 是阶段局部字段。冻结驱动末尾赋值表达式为 `phase == "pilot" and pilot_gate(episodes) and source_matches_finish`；formal 的 phase 条件直接为 false。正式启动前另行读取 pilot-v3 的 `summary.json`，验证 `pilot_gate_passed=true` 和相同 freeze_id 后才执行。formal `completed=true`、`source_matches_finish=true`，24/24 次完成。原字段保留，来源见 [驱动](../../scripts/benchmark_preference_memory.py)、[pilot-v3 汇总](../../results/runs/preference-memory-20261106-pilot-v3/summary.json)、[归档核验](../../results/runs/preference-memory-20261106-verification/audit.json)。

| 校准用量 | available 请求 / input / output / cache | empty 请求 / input / output / cache |
| --- | --- | --- |
| pilot | 14 / 56,268 / 568 / 35,200 | 32 / 170,274 / 1,483 / 107,520 |
| pilot-v2 | 17 / 71,607 / 758 / 52,736 | 19 / 81,165 / 891 / 60,672 |
| pilot-v3 | 12 / 50,562 / 540 / 33,536 | 18 / 77,505 / 770 / 49,408 |

三轮校准合计 12 次执行、业务成功 11/12：112 请求，507,381 input，5,010 output，339,072 cache。正式与校准总计 36 次执行、业务成功 33/36：333 请求，1,510,764 input，14,984 output，1,099,520 cache。

三轮 pilot 的失败模型请求、未知用量请求、基础设施异常均为 0。pilot 第一轮有 1 次业务失败，其 20 次模型请求全部保留。第一轮 summary 未设置 `source_matches_finish`，复算记录该项为 null；三轮 source.tar.gz 的源码成员哈希全部核验通过。v2、v3、formal 的完成时源码一致字段均为 true。

## Namespace 与适用范围

当前 scope 为 `service_url/projects/{project_id}/principals/{principal_id}/issues/{ticket_id}`，memory namespace 为 `("servicedesk", fingerprint(scope), "confirmed_preferences")`。同 scope 新 thread 可以读取确认偏好；换工单或 API 身份产生不同 namespace，需要独立确认或另行设计跨工单用户级记忆。当前实验度量同工单跨会话持久化，API 身份指调用身份。

focused tests 中的真实 SqliteStore 确定性测试验证关闭重开后同 scope 可读、换工单为空、换 API 身份为空，模型调用为 0；最新记录 [focused-5.xml](../../results/runs/preference-memory-20261106-tests/focused-5.xml) 为 **14 passed**。这些测试单独列为实现验证。

## 原始材料与复算

每次执行保留 `setup.json`、`before/after.json`、`http.json`、`writes.json`、`turns.json`、`sessions.json`、`model_calls.json`、`model_requests.json`、`memory_searches.json`、`tool_schemas.json`、`result.json`，以及真实 `state/memory.sqlite`、`state/sessions/*/checkpoint.sqlite` 和生产 trace。

根目录忽略规则覆盖 SQLite，因此每组另附可纳入仓库的 `raw-state.tar.gz` 与 `raw-state-manifest.json`。归档含原始状态字节，未重建数据库；36 份 memory.sqlite 与 36 份 checkpoint.sqlite 共 **72 份**，逐文件核对 SHA-256、从归档恢复读取、执行 SQLite integrity_check 并核对各表行数。原文件哈希保持一致。

| 材料 | 路径 |
| --- | --- |
| 正式 | [formal](../../results/runs/preference-memory-20261106-formal/)，[原始状态包](../../results/runs/preference-memory-20261106-formal/raw-state.tar.gz)，[成员清单](../../results/runs/preference-memory-20261106-formal/raw-state-manifest.json) |
| 第一轮校准 | [pilot](../../results/runs/preference-memory-20261106-pilot/)，[freeze](../../results/runs/preference-memory-20261106-freeze/) |
| 第二轮校准 | [pilot-v2](../../results/runs/preference-memory-20261106-pilot-v2/)，[freeze-v2](../../results/runs/preference-memory-20261106-freeze-v2/) |
| 第三轮校准 | [pilot-v3](../../results/runs/preference-memory-20261106-pilot-v3/)，[freeze-v3](../../results/runs/preference-memory-20261106-freeze-v3/) |
| 可复算实现 | [驱动](../../scripts/benchmark_preference_memory.py)，[六场景](scenarios.json)，[分析](analyze.py)，[归档校验](package.py)，[focused tests](../../tests/test_preference_memory_benchmark.py) |

三个 freeze 目录分别保留 freeze.json 和含 28 个实现/依赖声明文件的 source.tar.gz。正式冻结 ID 为 `71513fefdb3a214a4ebcb40ae987196578908f40d87053935348b1a6ba7b10d2`；基础 commit 为 `49850cd965ab19789abfa45fc654dc60acbcce45`，正式实现清单 SHA-256 为 `f3b42bc46d3b0ce60c66ca961a4b6e56f23b457dc6ad235acfaf19fb244636d5`。新增实验代码由源码归档和逐文件哈希定位。

复算使用对应轮次已验证的冻结源码包和场景；逐项核验协议、实现清单、源码包、包内成员、场景 SHA-256，以及运行时从工作区导入的生产源码。任何失配纳入 `checks.issues`，写出失败审核结果并以非零状态退出。每份输出记录 analyzer 自身 SHA-256。输出采用独立审核目录和排他创建，已有原始结果与审核文件保持原值。

以下命令只解析既有记录、写派生分析或校验归档，不调用模型或业务服务。示例使用新的 `audit-recheck` 目录；已完成的公开复算位于 [audit-public](../../results/runs/preference-memory-20261106-audit-public/)：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/preference-memory/analyze.py results/runs/preference-memory-20261106-formal --freeze results/runs/preference-memory-20261106-freeze-v3/freeze.json --output results/runs/preference-memory-20261106-audit-recheck/formal.json
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/preference-memory/analyze.py results/runs/preference-memory-20261106-pilot --freeze results/runs/preference-memory-20261106-freeze/freeze.json --output results/runs/preference-memory-20261106-audit-recheck/pilot.json
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/preference-memory/analyze.py results/runs/preference-memory-20261106-pilot-v2 --freeze results/runs/preference-memory-20261106-freeze-v2/freeze.json --output results/runs/preference-memory-20261106-audit-recheck/pilot-v2.json
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/preference-memory/analyze.py results/runs/preference-memory-20261106-pilot-v3 --freeze results/runs/preference-memory-20261106-freeze-v3/freeze.json --output results/runs/preference-memory-20261106-audit-recheck/pilot-v3.json
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/preference-memory/package.py --verify-only
```

四组分析检查均为 `issues=[]`：逐请求与 UsageRecorder、生产 trace 账本对应；usage 字段完整；HTTP/业务评分/澄清轮数可复算；两条件 Schema 与首请求内容一致。首请求比较排除 LangChain 本地随机 message id，模型输入文本保持逐对相同。归档校验包括三轮源码包哈希与 72 份真实数据库。各组状态包可在对应运行目录恢复，成员路径保持原结构；校验脚本也支持直接从归档读取状态。凭证只在本机加载，公开材料保留合成工单与服务身份。

分析器的独立校验为 [7 passed](../../results/runs/preference-memory-20261106-audit-final/analyzer-tests.xml)。[场景哈希失配](../../results/runs/preference-memory-20261106-audit-public/validation-scenario-rejection.json)和[源码成员哈希失配](../../results/runs/preference-memory-20261106-audit-public/validation-source-rejection.json)使用单独构造的测试记录，两条命令均写出对应 issues 并以退出码 1 结束；这两份文件是校验器拒绝错误来源的证据，与真实模型实验分账。
