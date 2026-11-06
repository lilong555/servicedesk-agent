# ServiceDesk 业务故障实验验证

正式实验：[报告](../business-reliability-20261106-001/report.md)、[汇总](../business-reliability-20261106-001/summary.json)、[204 个逻辑操作执行槽位](../business-reliability-20261106-001/operations.csv)。

主实验为 100 对独立合成备注操作，每组包含 50 个正常操作及 50 个提交后丢响应操作。普通至多重发一次基线与生产只读对账均完成 100/100；重复条数分别为 50/0，写请求分别为 150/100，总 HTTP 请求分别为 450/600，最终未知均为 0。生产方案持久化 100 条校验回执，基线持久化 100 条 HTTP 成功确认。

迟到提交及 checkpoint 间隙分别执行两种策略，共 4 个独立于主实验的确定性槽位。普通重试各产生 1 条重复备注，生产方案各保留 1 条已提交备注及可恢复回执。撤权另计 1 个确定性调度、4 个子进程，成功 revoke 后记录 4 次 HTTP 请求及 0 次 HTTP 写准入。

## 验证记录

| 检查 | 结果 | 原始记录 |
| --- | --- | --- |
| 新增回归初次执行 | 18 passed | [initial.xml](initial.xml)、[initial.log](initial.log) |
| 新增及生产 focused tests | 84 passed，1 条上游弃用警告 | [focused.xml](focused.xml)、[focused.log](focused.log) |
| Ruff | 通过 | [ruff.log](ruff.log) |
| 正式实验 | 协议验收通过，预期超时及退出码 86 已保留 | [run-001.log](run-001.log) |
| 原始 SQLite 独立核算 | 两组分别 150/100 个 journal、50/0 条重复 | [recompute-audit.json](recompute-audit.json) |
| 汇总重算 | summary 内容一致，842 个原始文件哈希一致 | [recompute.log](recompute.log)、[recompute-audit.json](recompute-audit.json) |
| 冻结源码及夹具 | 29 个文件与工作区及 source.zip 对应内容一致 | [freeze.json](../business-reliability-20261106-001/freeze.json) |
| 既有证据及脏文件 | 920 个受保护文件内容哈希一致 | [protected-files.json](protected-files.json) |

18 个新增回归已包含于后续 84 个 focused tests；测试执行数单独于业务逻辑操作数报告。弃用警告来自上游 trustcall 对 `langgraph.constants.Send` 的导入。测试临时目录保留了受控失败、原生 checkpoint 及子进程轨迹。

## 复现命令

工作目录：`/home/li/code/servicedesk-agent`。使用已安装仓库依赖的 `.venv`，业务 HTTP 均由 MockTransport 处理，会话模型均为 ScriptedChatModel。

```bash
PYTHONDONTWRITEBYTECODE=1 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false \
  .venv/bin/python scripts/benchmark_business_reliability.py run \
  --output results/runs/business-reliability-20261106-reproduce

PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  scripts/benchmark_business_reliability.py summarize \
  results/runs/business-reliability-20261106-001

mkdir -p results/runs/business-reliability-20261106-retest
PYTHONDONTWRITEBYTECODE=1 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false \
  .venv/bin/pytest -p no:cacheprovider \
  tests/test_business_reliability_benchmark.py \
  tests/test_operations.py tests/test_reconciliation.py tests/test_revocation.py \
  --basetemp=results/runs/business-reliability-20261106-retest/tmp \
  --junitxml=results/runs/business-reliability-20261106-retest/focused.xml

.venv/bin/ruff check --no-cache \
  scripts/benchmark_business_reliability.py tests/test_business_reliability_benchmark.py
```

每次完整执行使用新的输出目录名，已有实验目录保留。`summarize` 从原始 SQLite 和阶段记录重建派生表。

## 交付文件

- `scripts/benchmark_business_reliability.py`
- `benchmarks/business-reliability/protocol.json`
- `benchmarks/business-reliability/redmine.json`
- `tests/test_business_reliability_benchmark.py`
- `results/runs/business-reliability-20261106-001/`
- `results/runs/business-reliability-20261106-verification/`

生产核心、既有业务测试、其他实验脚本、README、简历及 Git 提交状态保持原样；`THIRD_PARTY_NOTICES.md` 和 `tools/` 的既有内容通过保护哈希核对。新增模型服务请求为 0，真实业务 HTTP 写入为 0。统计结论适用于本次合成服务及预设调度，既有真实 Redmine 撤权记录单独引用。
