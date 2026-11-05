# ServiceDesk Agent 结果卡

**默认 A，B 按需启用，C 保持关闭。** 固定开发集和冻结留出中 A/B 成功数相同，A 的 Agent 调用与逻辑输入较少。真实 Redmine 验收支持跨进程交接、停止恢复与会话写权限撤销。

验证日期：2026-11-05；真实模型：Azure CPA `gpt-6-luna`。

## 问题与实现

IT 服务台调查常跨班次、跨进程继续，期间负责人和政策会变化。Agent 需要保留用户授权、开放工单的约束和待办，依据最新业务证据行动，并提供可核对的写入结果。

执行循环复用 DeepAgents；原生 SQLite Checkpointer 保存任务合同、待办与回执；LangGraph SqliteStore 持久化写入意图。Redmine Adapter 在提交前校验授权、业务版本与工作流，提交后回读日志，未决提交通过只读对账恢复。业务界面直接使用 Redmine。

A 使用原生 Harness、完整必要政策和业务工具；B 增加原生 Skills、版本化政策检索与 LangMem 偏好查询。

## 关键验证

- **真实交接：工单 #6，九项检查通过。** 两个独立进程恢复同一 Checkpoint，保留开放约束和待办；新备注使用变更后的负责人、政策 v2，两次备注均有真实回执。合计 16 次真实模型请求。[验收原文](../results/runs/redmine-live-reliable-20261105/summary.json)
- **停止恢复：工单 #7，十六项检查通过。** 初始脚本模型留下排队调用，真实模型恢复时优先处理最新停止消息；恢复阶段零写入尝试，完整工单状态保持一致。[验收原文](../results/runs/cancellation-live-20261105/summary.json)
- **权限撤销：工单 #10，二十八项检查通过。** 四个进程验证原子缩权、旧调用取消、工具层拒绝写入与真实模型只读恢复；撤销后零 HTTP 写入尝试，回执与压缩状态完整保留。[验收原文](../results/runs/revocation-live-20261105/summary.json)
- **工程回归：525 项项目测试通过。** 写入故障、延迟提交和事务回滚由本地合成 HTTP 服务及原生状态回归覆盖。[测试记录](../results/runs/verification-revocation/project.xml)

## 固定 A/B 结果

固定 CRM-Agent-Bench 九题开发、十二题留出；两组使用相同模型、完整公开 SOP、业务环境和原生 Grader，留出按冻结协议执行。

| 批次 | 组别 | 成功 / 已开始 | Agent 调用 | Agent 逻辑输入 tokens | Agent 输出 tokens |
| --- | --- | ---: | ---: | ---: | ---: |
| 共同政策开发 | A | 8 / 9 | 46 | 181,758 | 1,346 |
| 共同政策开发 | B | 8 / 9 | 51 | 239,269 | 1,446 |
| 冻结留出 | A | 11 / 12 | 55 | 214,692 | 1,592 |
| 冻结留出 | B | 11 / 12 | 57 | 262,371 | 1,632 |

含用户模拟器，开发与留出分别使用 146、168 次外部模型请求；计划 episode 全部评分，基础设施异常均为零。缓存输入已包含在逻辑输入中。费用单价为空，费用字段为 `null`。

## 结论与证据

本轮留出逐题配对全部持平，支持继续采用组件配置较轻的 A。B 供版本化政策与交接需求选用；本轮成功率收益为零。C 保持关闭。开发与留出各组均保留一条原生失败，成绩、调用和费用按各批次独立记录。

原始入口：[开发配对汇总](../results/development-full-sop.json)、[留出配对汇总](../results/holdout.json)、[冻结协议](../results/runs/holdout-freeze-luna-20261105/freeze.json)、[完成登记](../results/runs/holdout-freeze-luna-20261105/execution.json)。逐题轨迹、失败原因及独立开发批次见[完整结果](results.md)与[原始记录索引](../results/README.md)。

[工单交接演示](demo.md)提供执行命令、阶段快照与回执核对入口。
