# 工单级确认偏好定向实验

六个合成开发场景比较“确认偏好可召回”与“约定空结果”。四个场景需要历史确认的编号、日志字段、沟通路由或审计格式；两个场景提供明确的新指令，观察旧偏好覆盖风险。每场景两条件、两次独立重复，共 24 次正式执行；每轮两场景配对 pilot 共 4 次，实际保留三轮、12 次校准执行，单独统计。

[结果与原始证据](results.md)：固定用户补充策略下精确匹配成功率为 available 10/12、empty 12/12。全部用户补充 0/9 轮，其中偏好补充 0/8 轮；固定偏好补充包含完整期望备注，结果包含用户帮助。两次格式失败及三轮 pilot 的 12 次执行、112 次请求完整保留，真实 SQLite 与全部用量可复算。

## 实验合同

两条件都使用生产 `execute_session`、DeepAgents、LangMem、SqliteStore 和 `ModelSettings` 构建的 `gpt-6-luna`，客户端 `max_retries=0`。业务服务为现有 `tests/conftest.py::RedmineFixture`，通过真实 loopback HTTP 提供 Redmine 协议；场景数据及服务身份均为合成数据。

两条件的工具 Schema、Skills、政策、业务权限完全相同。实验层在 `recall_confirmed_preferences` 入口调用原生 LangMem 查询，然后按条件返回真实结果或 JSON 空数组。两条件共享参数说明：namespace 已限定工单与 API 身份，使用 `filter=null` 读取其中的确认偏好；生产记录为嵌套 Evidence 数据。Skills 文件的生成时间戳固定，内容采用生产资产。

建立阶段通过生产 `remember_confirmed_preference` 写入相同的显式确认文本，关闭 SQLite Store。后续阶段重新打开 Store，使用相同服务、项目、API 身份和工单 scope，以及新的 thread。建立阶段采用直接确认写入 API，模型请求为零；没有建立阶段对话、交接摘要、用户约束或旧 journal 进入新会话。只有偏好工具的返回内容携带历史偏好，当前指令覆盖场景则显式提供新要求。

每个场景、条件和重复拥有独立 SQLite Store 与全新业务状态。一个运行目录内使用同一 HTTP 服务地址，各场景的两次重复交替 available/empty 与 empty/available 顺序。服务端提示缓存保持实际运行状态，逐请求记录 cache read tokens；应用状态重置与服务端缓存分别计量。

## 评分与分母

固定用户补充策略下的精确匹配成功要求真实 HTTP 只写入一条备注，正文在空白规范化后符合冻结期望，形成一个 journal 和经过回读的有效回执，其他工单字段保持原值。空结果组可以合理询问，冻结的关键词分类器按问题类型提供固定答复，其中偏好补充含完整期望备注；结果包含该用户帮助，补充之后的模型与业务调用全部计入。首次业务写入后停止补充，保留错误写入的原始结果。

主要指标是四个历史偏好场景中的偏好澄清/补充次数，分母按每条件全部已开始执行计算。任务成功、覆盖场景的新指令优先级、Agent 模型调用及逻辑 input/output/cache tokens 单列。固定用户脚本没有外部模型调用。基础设施失败保留为 `success=null`，同时列出已开始、已评分、未开始和未知 usage；成功率的主分母为已开始次数。

六个场景属于合成开发数据，两次重复提供独立状态重置后的观察，不增加独立场景数。统计表达限定到当前模型、生产会话入口、合成业务服务与冻结模板。

## 复现

凭证由本机 `.runtime/cpa.env` 加载，结果只保存模型名与公开推理参数。先完成 focused tests，再在可用 CPA 隧道上执行；命令使用新的输出目录，已有目录会被拒绝覆盖。

```bash
mkdir -p results/runs/preference-memory-20261106-tests-new
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  tests/test_preference_memory_benchmark.py \
  --basetemp=results/runs/preference-memory-20261106-tests-new/tmp \
  --junitxml=results/runs/preference-memory-20261106-tests-new/focused.xml

PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/cpa.py status
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/benchmark_preference_memory.py freeze \
  --output results/runs/preference-memory-20261106-freeze-new
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/benchmark_preference_memory.py run \
  --phase pilot --freeze results/runs/preference-memory-20261106-freeze-new/freeze.json \
  --output results/runs/preference-memory-20261106-pilot-new
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/benchmark_preference_memory.py run \
  --phase formal --freeze results/runs/preference-memory-20261106-freeze-new/freeze.json \
  --pilot results/runs/preference-memory-20261106-pilot-new \
  --output results/runs/preference-memory-20261106-formal-new
```

冻结记录包含六场景数据哈希、实现文件哈希、依赖版本、模型参数、完整调度与评分规则。正式组要求同一冻结下 pilot 四次完整评分、usage 完整、两种查询分支实际执行、两条件各至少一次有回执的正确写入，以及首个模型请求没有历史答案泄漏。正常计分的业务失败继续保留。原始记录包括显式确认、Store、Checkpoint、生产会话 trace、模型请求与 UsageRecorder 回调、HTTP 请求和写入、前后业务状态、逐轮用户交互及逐次结果。

## 既有数据复算

以下命令解析已有正式组并校验四组真实 SQLite、三轮源码包，不发起模型调用或业务请求。分析器校验冻结源码与场景哈希，失配写入 issues 并非零退出；记录自身 SHA，输出写入新的独立目录。已完成审核见 [audit-public](../../results/runs/preference-memory-20261106-audit-public/)。各运行目录的 `raw-state.tar.gz` 保留被根目录忽略规则覆盖的真实 SQLite 与 Checkpoint；成员哈希和表行数见同目录 `raw-state-manifest.json`。

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/preference-memory/analyze.py \
  results/runs/preference-memory-20261106-formal \
  --freeze results/runs/preference-memory-20261106-freeze-v3/freeze.json \
  --output results/runs/preference-memory-20261106-audit-recheck/formal.json
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python benchmarks/preference-memory/package.py --verify-only
```
