# Memory 公开复算

本目录仅包含既有记录的复算结果，模型与业务服务调用为 0。原始结果保留在对应 formal/pilot 目录。

| 文件 | 内容 |
| --- | --- |
| [formal.json](formal.json) | 24 次正式执行，221 请求，固定用户补充策略下精确匹配 10/12 对 12/12；全部用户补充 0/9，偏好补充 0/8 |
| [pilot.json](pilot.json) | 第一轮开发校准：4 次、46 请求 |
| [pilot-v2.json](pilot-v2.json) | 第二轮开发校准：4 次、36 请求 |
| [pilot-v3.json](pilot-v3.json) | 第三轮开发校准：4 次、30 请求 |
| [validation-scenario-rejection.json](validation-scenario-rejection.json) | 单独构造的场景哈希失配，预期退出码 1 |
| [validation-source-rejection.json](validation-source-rejection.json) | 单独构造的源码成员哈希失配，预期退出码 1 |

四份真实数据复算均为 `checks.issues=[]`，每份记录 analyzer 自身 SHA 与冻结来源核验。两份 validation 文件验证错误来源被拒绝，属于零模型校验材料。

固定偏好补充含完整期望备注，结果包含用户帮助；两次精确匹配失败完整保留。三轮 pilot 合计 12 次执行、112 请求，改 gate 和加入两条件共同 `filter=null` 指导的说明，以及真实 SQLite、源码归档、用量总账见[完整报告](../../../benchmarks/preference-memory/results.md)。
