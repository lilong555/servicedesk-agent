# ServiceDesk 业务故障与撤权实验

本报告来自固定合成 Redmine REST 服务、生产 RedmineClient/SqliteStore 与会话恢复路径。
主实验含 100 对独立逻辑备注操作；每种策略 50 个正常操作、50 个提交后丢响应操作。
结果按逻辑操作计量，描述该固定故障机制；真实 Redmine 撤权证据保持独立。

| 策略 / 子集 | 操作 | HTTP请求 | 写请求 | 提交/journal | 完成 | 恰好一次 | 重复条数 | 最终未知 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| at_least_once / normal | 50 | 150 | 50 | 50 | 50 | 50 | 0 | 0 |
| at_least_once / fault | 50 | 300 | 100 | 100 | 50 | 0 | 50 | 0 |
| at_least_once / all | 100 | 450 | 150 | 150 | 100 | 50 | 50 | 0 |
| reconcile / normal | 50 | 300 | 50 | 50 | 50 | 50 | 0 | 0 |
| reconcile / fault | 50 | 300 | 50 | 50 | 50 | 50 | 0 | 0 |
| reconcile / all | 100 | 600 | 100 | 100 | 100 | 100 | 0 | 0 |

## 回执与未知状态

基线持久化 HTTP 成功确认；生产方案持久化经过 journal 校验的回执。同一逻辑操作的额外 journal 计为重复条数，HTTP 确认与已验证回执分别保留。
- at_least_once：HTTP 确认 100，生产校验回执 0，唯一 journal 证据 50/100；阶段末曾未知 50/100，未知持续 50 逻辑 tick。
- reconcile：HTTP 确认 0，生产校验回执 100，唯一 journal 证据 100/100；阶段末曾未知 0/100，未知持续 0 逻辑 tick。

逻辑 tick 表示预设调度间隔；本实验的耗时口径为逻辑调度，网络延迟与墙钟 MTTR 留待独立采集。主实验丢失率为首发操作的 50/100，重发响应均正常送达。

## 确定性恢复

| 案例 | 策略 | 写请求 | journal | 重复 | 完成 | 最终未知 |
| --- | --- | ---: | ---: | ---: | --- | --- |
| checkpoint_gap | at_least_once | 2 | 2 | 1 | True | False |
| checkpoint_gap | reconcile | 1 | 1 | 0 | True | False |
| late_commit | at_least_once | 2 | 2 | 1 | True | False |
| late_commit | reconcile | 1 | 1 | 0 | True | False |

迟到提交在 tick 2 释放，观察至 tick 3；checkpoint 间隙通过进程退出码 86 注入。各案例都从新 Python 进程打开持久状态，生产案例执行 execute_session(resume=True) 与 reconcile_ticket_operations。每个案例每种策略各 1 次，独立于主实验比例。

## 撤权调度

1 个确定性调度、4 个子进程：持久化两个排队写工具，锁内撤权返回 session_busy，释放锁后撤权事务成功，新进程拒绝旧授权并按持久只读合同恢复。
撤权成功返回之后记录 4 次 HTTP 请求、0 次 HTTP 写准入。两个排队写调用取消，两个新写探测按授权拒绝；撤权时在途写为 0。
该调度测量排队工具与进程恢复行为。已执行写入的抢占语义另行验证；检查项数量单独保留。

既有真实 Redmine 撤权记录：[revocation-live-20261105](../revocation-live-20261105)，本次合成数据单独计量。

## 复现与核算

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/benchmark_business_reliability.py run --output results/runs/business-reliability-20261106-reproduce
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/benchmark_business_reliability.py summarize results/runs/business-reliability-20261106-001
```

freeze.json 固定协议、夹具、源码 SHA 与依赖版本；source.zip 保存执行源码。每个操作保留 case.json、server.sqlite、events.jsonl、阶段结果和生产 memory.sqlite；会话案例另保留原生 checkpoint、trace、子进程退出和标准输出/错误。
协议验收：通过。全部运行及故障轨迹保留；失败进入原定分母。
