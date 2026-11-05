# 评测协议

评测包含原生框架接线、真实工单交接、固定 CRM 开发任务和冻结后的留出任务。模型效果使用真实服务调用，确定性脚本用于接口与验收链路回归。

## 固定任务

基准：`salesforce-misc/crm-agent-bench`。

源码：`fc9575cf9bda085aa1f66d920fea944588005964`。

任务清单：`src/servicedesk_agent/assets/itsm_subset.json`。加载时核对任务文件、政策文件 SHA256，以及报告指定的 21 个 execution-only 任务。划分固定为 9 道开发题和 12 道留出题。

三道校准任务：

1. `itsm_hours_005_add_comment_simple`
2. `itsm_ops_007_update_status_to_in_progress`
3. `itsm_ops_012_comment_then_status_update_order`

校准题属于开发集。重复运行的结果分别保留，统计时区分运行批次与独立任务数量。

## 分组

| 条件 | A | B |
| --- | --- | --- |
| DeepAgents 原生 Loop / 压缩 / Checkpoint | 相同 | 相同 |
| 业务工具、完整必要政策、公开业务 SOP、初始状态 | 相同 | 相同 |
| 模型、温度、响应上限、步数限制 | 相同 | 相同 |
| 官方 `react` 用户模拟器 | 相同模型 | 相同模型 |
| 原生 Skills、BM25 和 Memory 查询工具 | 关闭 | 开启 |
| 每题状态与 Memory namespace | 独立 | 独立 |

B 增加的工具及 Skill 读取会进入真实工具轨迹。严格顺序验收保留这些调用。每组同样保留完整的必要政策。

共同政策由固定源码的 `src/tasks/policies/itsm_policy.md` 和 `src/agents/itsm.yaml` 中三个公开业务节点的 `description`、`instructions` 组成：工单处理、记录删除安全、业务时间规则。两组使用相同原文与适用说明，运行架构继续使用原生 DeepAgents。配置中的 `policy_profile`、`policy_sources` 与 `policies.json` 记录具体来源及内容指纹，B 的检索目录分别保存这四份文档。合同依据与原生重放证据见 [删除合同审计](benchmark-contract.md)。

C 策略依据开发任务和交接回归中的具体失败确定；当前实现提供 A/B。

## 冻结与留出

完成开发验证后，冻结当前源码、全部已安装包版本、模型参数、调用和回合上限、A/B 顺序、十二题清单及政策来源。冻结过程读取任务清单与公开政策，模型调用数为零。

```bash
.venv/bin/desk-agent --env-file .runtime/cpa.env bench freeze \
  --groups A B --max-calls 600 --max-turns 15 \
  --output artifacts/holdout-freeze

.venv/bin/desk-agent --env-file .runtime/cpa.env bench run \
  --split holdout --groups A B --max-calls 600 --max-turns 15 \
  --freeze artifacts/holdout-freeze/freeze.json \
  --output artifacts/holdout-evaluation
```

冻结目录包含 `freeze.json` 和 `source.tar.gz`。`freeze_id` 是协议内容的 SHA-256；评测开始前校验当前源码、依赖、模型、政策配置和归档。每个 episode 开始前再次核对运行时和 CRM 源码，完成时再验证一次。

同一冻结目录通过独占创建的 `execution.json` 记录一次执行。状态包括 `running`、`completed`、`partial` 和 `interrupted`，中断和调用上限保留原始执行记录。后续独立复现实验使用新的冻结目录，并作为独立批次报告。模型访问密钥沿用本机环境，凭证轮换保持协议参数有效。

结果目录保留冻结协议副本。对已登记的策略和任务执行一次完整评测，所有开始过的尝试及失败均进入对应批次。留出评测期间固定策略；结果用于检验已冻结方案。

## 成功与用量

任务成功由原生动作、参数、调用顺序、禁止动作、最终状态和禁止状态验收共同计算。核心 Grader 保持上游原样。模型与业务错误保留在对应任务记录中。

结果同时提供 `success_per_started` 和 `success_per_scored`。前者包含全部已开始尝试；后者遵循原生已评分任务口径。基础设施异常单独计数。

`planned_episodes`、`started_episodes`、`unstarted_episodes` 和 `completed` 表示计划覆盖情况。配对统计以完整的计划任务数计算未评分对，提前结束时保留未开始组别。

模型调用按 Agent 与用户模拟器分开记录，包括失败请求、缺失 usage、逻辑输入、缓存输入、输出和耗时。缓存输入属于逻辑输入的一部分。原生摘要调用通过同一回调记录。记忆写入采用明确用户提交，维护过程无需模型调用。

费用根据显式配置的单价计算。缺少价格或 usage 时，相关费用字段为 `null`。每成功任务成本的分子包括全部尝试的 Agent 成本；零成功时该值为 `null`。用户模拟器成本另列并计入实验总成本。

调用上限覆盖 Agent 与用户模拟器；基准选定子集的语义 Judge 为零次。

## 原始产物

```text
config.json
source.tar.gz
policies.json
episodes.json
summary.json
freeze.json（冻结留出运行）
A/或B/任务ID/result.json
A/或B/任务ID/model_calls.json
A/或B/任务ID/checkpoint.sqlite
```

`config.json` 记录模型配置、任务顺序、源码校验信息、依赖版本、政策来源和限制。`result.json` 保留对话轨迹、工具事件、初始状态、最终状态及每项原生 Grader 输出。

随机种子用于任务与本地 RNG；服务端采样 seed 保持默认。对小样本使用成功数与配对胜负平描述结果。

## 生命周期回归

| 场景 | 验收方式 |
| --- | --- |
| 负责人变更 | 旧指纹拒绝、窄字段保留、新负责人进入恢复备注 |
| 依赖变化 | 相同时间戳下的新增、删除、改指向均改变源版本 |
| 关键约束跨压缩 | 原生 Summarization 后从 SQLite 恢复合同与待办 |
| 政策版本变化 | 当前版本过滤、历史原文、真实恢复时使用 v2 |
| 身份与会话隔离 | API 用户、项目、工单、会话及 Memory namespace |
| 原始证据回读 | 原生历史文件落盘与 `read_file` 完整回读 |

真实 Redmine 演示使用两个独立进程，并通过管理员 API 在进程之间改变负责人、发布新政策。验收读取实际工单和新写入的日志，同时核对模型读取到的政策证据。

## 下一阶段

开发集用于定位业务与接线错误。策略冻结后再开放一次性留出评估，并保留原生方案通过、组件无收益和成本增加等结果。新增生命周期逻辑的保留依据为可复现的业务可靠性收益。
