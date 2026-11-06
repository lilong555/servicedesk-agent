# 原生轨迹归档复算

分析器直接读取 `raw-state.tar.gz`，逐项校验包哈希、成员清单、成员字节与路径类型，
随后将原生轨迹的模型请求 ID 与 UsageRecorder 对账。运行目录保留归档即可执行复算，
本地展开的 `state/` 可独立存放。

| 结果 | 执行数 | 模型请求 | 复算 issues |
| --- | ---: | ---: | --- |
| [formal.json](formal.json) | 24 | 221 | 空 |
| [pilot.json](pilot.json) | 4 | 46 | 空 |
| [pilot-v2.json](pilot-v2.json) | 4 | 36 | 空 |
| [pilot-v3.json](pilot-v3.json) | 4 | 30 | 空 |

表中请求来自原始实验账本；本次离线复算调用模型和业务服务的次数均为零。
每份记录包含分析器 SHA、冻结源码及场景身份、归档绑定、业务计分和逐请求用量。
正式组的精确匹配结果保持 10/12 对 12/12，偏好补充保持 0 对 8。

[实验结果与复现命令](../../../benchmarks/preference-memory/results.md)。
