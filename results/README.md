# 验证记录

本目录保存 2026-11-05 的开发验证数据。业务内容来自隔离 Redmine 测试项目与固定 CRM-Agent-Bench 合成任务。

| 记录 | 入口 |
| --- | --- |
| 九题开发集配对汇总 | [development.json](development.json) |
| 三题校准配对汇总 | [calibration.json](calibration.json) |
| 开发集 A 原始轨迹 | [episodes.json](runs/development-luna-A-verified/episodes.json) |
| 开发集 B 原始轨迹 | [episodes.json](runs/development-luna-B-verified/episodes.json) |
| 真实模型工单交接 | [summary.json](runs/redmine-live-luna-complete/summary.json) |
| 确定性接线与负对照 | [wiring-20261105](runs/wiring-20261105) |
| 项目测试 | [project.xml](runs/verification/project.xml) |
| 指定上游测试 | [upstream.xml](runs/verification/upstream.xml) |

`runs/` 保留 JSON 轨迹、模型调用用量、配置、已有源码快照和测试 XML。SQLite、运行凭证与锁文件保留在本地。早期连接诊断批次使用独立目录，统计口径见 [验证结果](../docs/results.md)。

原始记录保留运行时的目录引用，用于关联 Checkpoint、轨迹和源码指纹。公开副本位于 `results/runs/`；顶层汇总的输入路径相对于仓库根目录，`episodes_sha256` 可用于校验对应原始文件。

两组开发任务使用相同的源码快照指纹。公开发布的文档与运行时快照分别保留，轨迹中的政策、Grader 结果、失败记录和模型用量保持原值。单价字段为空，费用使用 `null`。

CRM 衍生任务内容与轨迹遵循 [第三方声明](../THIRD_PARTY_NOTICES.md) 中的来源和许可。
