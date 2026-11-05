# 架构说明

ServiceDesk Agent 围绕一张工单执行具有明确授权的任务。默认方案 A 使用原生 DeepAgents，方案 B 增加版本化政策、两项业务 Skill 和明确确认的长期偏好。

## 状态与来源

| 数据 | 权威来源 | 持久化位置 |
| --- | --- | --- |
| 当前工单、负责人、状态、依赖、操作日志 | Redmine API | Redmine |
| 用户授权、关键约束、待办、回执引用 | 任务合同与真实工具结果 | 原生 SQLite Checkpointer |
| 写入意图、未决提交、已确认日志标识 | 业务 Adapter 与真实 Redmine 日志 | 原生 SqliteStore 独立 namespace |
| 当前及历史政策 | 受信任的版本化政策目录 | JSON 目录与 Haystack 内存索引 |
| 明确确认的偏好 | 用户显式提交 | LangMem 与 SqliteStore |

`Evidence` 包含来源 URI、来源种类、对象范围、源版本、观察时间、生效区间和内容。工单版本是 API 响应的规范化内容指纹，可以检测时间戳相同的负责人或依赖变化。政策版本包含发布版本与内容指纹。

政策检索先按 scope 和生效区间筛选，再使用 BM25。指定历史版本时可以读取完整原文。SQLite Memory 使用 namespace 和更新时间查询，索引配置保持为空。

## 执行路径

```text
CLI 明确授权
  -> API 身份核验与会话锁
  -> 原生 DeepAgents / SQLite Checkpointer
  -> 当前工单和适用政策读取
  -> 写入前校验范围、授权、源版本与工作流
  -> 窄字段 REST 写入
  -> 工单与操作日志回读
  -> 持久化回执、待办与交接摘要
```

会话标识由服务、项目、API 用户、工单和用户指定的会话名生成。同一会话采用跨进程单写者锁。恢复时校验完整任务合同，最新用户消息先进入下一次模型决策。原生状态更新保留已完成的并行工具结果，取消旧待执行调用，并通过结构化结果保持调用配对。随后依据最新用户要求重新规划。

备注和状态操作携带稳定的操作标识。HTTP 提交前，业务 Adapter 在原生 Store 中持久化一个会话级意图槽；结果未知时只读对账，确认前保留写入阻塞。操作序列由进程内锁和按会话的文件锁串行保护。

确认丢失时，通过完整备注指纹、作者、编辑者、新日志 ID 和操作标识恢复回执。状态操作同时检查真实 `status_id` 变更明细与当前状态。只读工具 `reconcile_ticket_operations` 可恢复已写入 Store、尚未进入 Checkpoint 的回执。`verified_receipts` 由业务 Adapter 写入，模型的自然语言完成声明单独保存。

读取版本与提交 PUT 之间存在跨客户端并发窗口。当前实现通过单会话串行执行、窄字段更新和回读校验保护演示流程；部署到多人同时修改的环境时，同字段原子冲突控制应由后端条件写入或锁机制承担。

## 模块

| 模块 | 职责 |
| --- | --- |
| `native.py` | 固定原生 Harness，关闭宿主执行工具与通用子 Agent |
| `agent.py` | 业务工具、任务状态、原生 Command / ToolMessage |
| `redmine.py` | 真实 REST 适配、授权和版本校验、回执 |
| `operations.py` | 复用原生 Store 的业务意图记录与会话写入锁 |
| `session.py` | 会话锁、原生恢复、执行轨迹 |
| `knowledge.py` | 政策版本、生效区间、BM25、完整原文 |
| `components.py` | 原生 Skills、LangMem 与 SqliteStore 接线 |
| `mcp_server.py` | 官方 stdio 只读业务服务 |
| `benchmark.py` | 原生 CRM 工具与 Grader 适配 |
| `evaluation.py` | 固定任务运行、模拟器、调用限制与结果汇总 |
| `provenance.py` | 评测源码快照与依赖版本 |
| `protocol.py` | 留出配置冻结、运行时校验与一次性执行记录 |
| `demo.py` | 独立进程交接与真实 Redmine 验收 |

业务 Skill 作为只读虚拟文件进入原生 StateBackend。默认 Agent 文件系统处于 Checkpoint 中，业务工具作用范围由任务合同与 Redmine 权限共同决定。

## 依赖

| 组件 | 固定版本 |
| --- | --- |
| Python | 3.12 |
| DeepAgents | 0.7.32 |
| LangChain / Core | 1.4.9 / 1.7.6 |
| LangGraph | 1.2.20 |
| langgraph-checkpoint-sqlite | 3.1.3 |
| LangMem | 0.0.30 |
| Haystack | 3.4.1 |
| MCP / LangChain MCP adapters | 1.30.0 / 0.3.2 |
| LangChain OpenAI / OpenAI SDK | 1.7.3 / 2.45.0 |
| Redmine 演示镜像 | 6.1.6-alpine |

安装入口为 `uv.lock`。CRM 使用固定源码 editable 安装，使原生任务数据和配置保持可访问。

## 运行安全

工单和检索内容作为业务数据处理。授权来源为显式 CLI 参数与持久化合同。默认只读，备注与状态转换分别授权，关闭动作另有布尔授权。

密钥使用 `SecretStr` 配置，运行配置与源码快照排除密钥文件。模型客户端使用独立 HTTP 传输；循环中的每个角色、每个 episode 独立管理连接。连接本机 CPA 时跳过环境 HTTP 代理。

服务当前部署于隔离测试项目。上线前应配置组织身份、真实政策发布者、业务权限、数据保留规则和后端并发写入合同。
