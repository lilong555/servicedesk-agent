# 验证记录

本目录保存 2026-11-05 的开发评测、冻结留出、真实工单验收与工程验证数据。业务内容来自隔离 Redmine 测试项目与固定 CRM-Agent-Bench 合成任务。各批次按政策配置与源码指纹独立归档。

| 记录 | 入口 |
| --- | --- |
| 冻结留出，A/B 各 11/12，168 次请求，24 个 episode 全评分 | [holdout.json](holdout.json)、[summary.json](runs/holdout-luna-20261105/summary.json) |
| 留出冻结协议与完成登记 | [freeze.json](runs/holdout-freeze-luna-20261105/freeze.json)、[execution.json](runs/holdout-freeze-luna-20261105/execution.json) |
| 完整公开 SOP 开发集，A/B 各 8/9，146 次请求 | [development-full-sop.json](development-full-sop.json) |
| 公开删除 SOP 开发集，A/B 各 8/9，140 次请求 | [development-delete-sop.json](development-delete-sop.json) |
| 原始开发集，A 8/9、B 7/9，145 次请求 | [development.json](development.json) |
| 三题校准配对汇总 | [calibration.json](calibration.json) |
| 完整公开 SOP 开发轨迹与来源配置 | [episodes.json](runs/development-full-sop-luna-20261105/episodes.json)、[config.json](runs/development-full-sop-luna-20261105/config.json) |
| 公开删除 SOP 开发轨迹与来源配置 | [episodes.json](runs/development-sop-luna-20261105/episodes.json)、[config.json](runs/development-sop-luna-20261105/config.json) |
| 原始开发集 A 轨迹 | [episodes.json](runs/development-luna-A-verified/episodes.json) |
| 原始开发集 B 轨迹 | [episodes.json](runs/development-luna-B-verified/episodes.json) |
| 删除合同原生重放审计 | [delete-contract-audit-20261105.json](delete-contract-audit-20261105.json) |
| 真实模型工单 #6 交接，九项通过、16 次请求 | [summary.json](runs/redmine-live-reliable-20261105/summary.json) |
| 脚本模型工单 #5 交接，九项通过、零外部模型请求 | [summary.json](runs/redmine-scripted-reliable-20261105/summary.json) |
| 工单 #7 停止恢复，十六项通过、恢复 3 次请求、零写入 | [summary.json](runs/cancellation-live-20261105/summary.json) |
| 工单 #8 脚本模型停止恢复，十六项通过、零外部模型请求 | [summary.json](runs/cancellation-scripted-final-20261105/summary.json) |
| 早期真实模型工单 #4 交接 | [summary.json](runs/redmine-live-luna-complete/summary.json) |
| 确定性接线与负对照 | [wiring-20261105](runs/wiring-20261105) |
| 当前项目测试，402 passed | [project.xml](runs/verification-final/project.xml) |
| 可靠性验证批次 | [project.xml](runs/verification-reliable/project.xml) |
| 最终指定上游测试，663 passed，包含 555 条 gold paths | [upstream.xml](runs/verification-final/upstream.xml) |
| 早期项目测试，240 passed | [project.xml](runs/verification/project.xml) |

完整公开 SOP 开发批次与留出的 `policy_profile` 为 `shared-policy-and-public-workflows`；公开删除 SOP 开发批次为 `shared-policy-and-public-deletion-workflow`。完整公开 SOP 开发批次的共同失败来自 `itsm_safety_012_arg_precision_delete_ticket1004` 的首次权限检查参数验收，两组最终删除成功，原生任务评分保持失败。详细统计与失败依据见 [验证结果](../docs/results.md)。

十二题留出已完成，零基础设施异常，B 相对 A 为 0 胜、0 负、12 平。两组各一条原生失败与冻结策略原样保留。冻结 ID 为 `b51b5d7b0a13df4e9a5927673bbb92fd43fac0061bb5a5b9705213b209e9c46a`。公开的 [holdout.json](holdout.json)、[运行目录](runs/holdout-luna-20261105) 和 [冻结目录](runs/holdout-freeze-luna-20261105) 分别保存配对汇总、运行结果和协议。默认方案为 A：本批次成功率持平，A 的 Agent 调用与逻辑输入较少，组件配置较轻；B 按业务需求启用，C 保持关闭。

`runs/` 保留 JSON 轨迹、模型调用用量、配置、已有源码快照和测试 XML。SQLite、运行凭证与锁文件保留在本地。早期连接诊断批次使用独立目录，统计口径见 [验证结果](../docs/results.md)。

原始记录保留运行时的目录引用，用于关联 Checkpoint、轨迹和源码指纹。公开副本位于 `results/runs/`；顶层汇总的输入路径相对于仓库根目录，`episodes_sha256` 可用于校验对应原始文件。

每个配对开发批次的 A/B 使用相同源码快照指纹；各独立批次保留各自的指纹与配置。公开发布的文档与运行时快照分别保留，轨迹中的政策、Grader 结果、失败记录和模型用量保持原值。单价字段为空，费用使用 `null`。

CRM 衍生任务内容与轨迹遵循 [第三方声明](../THIRD_PARTY_NOTICES.md) 中的来源和许可。
