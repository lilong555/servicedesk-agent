# ServiceDesk Agent

基于 DeepAgents 的 IT 服务台工单处理与跨会话交接 Agent。使用当前工单与政策证据继续任务，通过原生 SQLite Checkpointer 保留用户约束、待办和经过回读验证的操作回执，通过 LangGraph SqliteStore 持久化业务写入意图与紧凑回执。

业务界面使用 Redmine，模型通过 OpenAI-compatible 接口配置，执行环境为 Python 3.12 / Linux 或 WSL2。本次真实模型验证使用 Azure CPA 的 `gpt-6-luna`。

## 已实现

- 工单读取、授权备注与状态转换、操作结果回读验证。
- 跨进程任务恢复，最新用户输入优先于旧的排队工具调用，保留约束、待办、来源引用和已完成结果。
- 写入前校验业务版本、用户授权与工作流，并持久化操作意图；未决提交按会话阻止自动重发。
- 只读回执对账与 Checkpoint 回填，覆盖确认丢失、延迟落地和已完成回执恢复。
- 可选的版本化政策检索、历史原文、原生 Skills 和明确确认的长期偏好。
- 官方 MCP stdio 只读服务，以及固定 CRM 任务的 A/B 评测、协议冻结和分角色用量统计。

项目测试 **402 项通过**；真实 Redmine #6 双进程交接 **9 项检查通过**；#7 停止恢复 **16 项检查通过，恢复阶段零写入**。

共同政策九题开发集 **A/B 各 8/9，146 次模型请求**；冻结十二题留出集 **A/B 各 11/12，168 次请求，24 个 episode 全部评分，零基础设施异常**。留出配对结果为 0 胜、0 负、12 平。相同成功率下，A 的 Agent 调用和逻辑输入较少，组件配置较轻，继续作为默认方案；B 按政策检索与交接需求启用，C 保持关闭。原始 A 8/9、B 7/9 开发批次独立保留。完整记录见 [验证结果](docs/results.md)。

## 启动

安装需要 Git、Python 和 Docker Engine 或 Docker Desktop：

```bash
git clone https://github.com/lilong555/servicedesk-agent.git
cd servicedesk-agent
bash scripts/bootstrap.sh
.venv/bin/python scripts/redmine.py up
```

`bootstrap.sh` 安装锁定依赖并检出固定的 CRM 源码。Redmine 使用 Docker Engine 或 Docker Desktop；WSL2 下会自动识别 Windows Docker CLI。

Redmine 地址为 [http://localhost:3080](http://localhost:3080)，账号为 `desk.agent`，随机密码保存在 `.runtime/redmine-access.json`。容器仅监听本机，数据保存在项目专用 Docker volume。

### 模型配置

在当前终端配置支持 Chat Completions 工具调用的模型服务：

```bash
export DESK_MODEL='your-model-id'
export DESK_MODEL_BASE_URL='https://your-model-service.example/v1'
read -r -s -p 'Model API key: ' DESK_MODEL_API_KEY
export DESK_MODEL_API_KEY
.venv/bin/desk-agent --env-file .runtime/redmine.env doctor --ticket 1
```

也可以通过 `--env-file` 载入本地配置，完整字段见 [.env.example](.env.example)。后续命令使用当前终端的模型配置。

### Azure CPA

已配置 CPA 的环境可以使用以下连接入口：

```bash
.venv/bin/python scripts/cpa.py connect
.venv/bin/python scripts/cpa.py probe
.venv/bin/desk-agent \
  --env-file .runtime/redmine.env \
  --env-file .runtime/cpa.env doctor --ticket 1
```

CPA 连接复用 SSH 主机别名 `azure`，通过本机 `18317` 端口转发远端 `8317`。隧道由 `systemd --user` 临时服务管理，访问凭证保存在 `.runtime/cpa.env`，文件权限为 `0600`。`--host`、`--model`、`--local-port` 和 `--remote-port` 支持显式配置；更换隧道参数前执行 `scripts/cpa.py stop`。

使用 CPA 时，在下方的 `desk-agent` 命令中附加 `--env-file .runtime/cpa.env`，位置在子命令之前。

## 交接演示

```bash
.venv/bin/desk-agent demo --live --output artifacts/my-live-handoff
```

演示创建一张独立测试工单。第一个进程调查、备注并保存未完成事项；进程结束后，管理员更换负责人、发布政策 v2；第二个进程恢复原生 Checkpoint 并继续处理。

验收覆盖独立进程、同一 Checkpoint、当前负责人、开放状态、用户约束、待办、两条真实回执以及新政策的读取与使用。`summary.json`、两个阶段的结果和完整执行轨迹保存在指定目录。每次运行使用一个新的输出目录。

使用确定性脚本模型执行同一条真实 Redmine 接线验证：

```bash
.venv/bin/desk-agent demo --output artifacts/my-scripted-handoff
```

### 停止恢复验收

```bash
.venv/bin/python scripts/check_live_cancellation.py \
  --env-file .runtime/cpa.env \
  --access .runtime/redmine-access.json \
  --live --output artifacts/my-stop-resume
```

验收在隔离 Redmine 项目中创建测试工单。初始进程使用脚本模型留下未执行工具调用，恢复进程使用真实模型处理“停止写入”的新输入。检查覆盖首个模型请求中的最新输入、旧调用取消、只读证据、完整工单状态和交接记录。已归档的 #7 恢复阶段使用 3 次模型请求，写入尝试为零。

## 处理工单

先读取工单，再创建具有明确授权的会话：

```bash
.venv/bin/desk-agent --env-file .runtime/redmine.env ticket --ticket 1

.venv/bin/desk-agent \
  --env-file .runtime/redmine.env \
  start --ticket 1 --session vpn-review --allow-notes \
  --constraint '先调查并备注，保持工单开放。' \
  --message '调查 VPN 断连，按当前规程添加调查备注，保存待办后暂停。'
```

跨进程继续处理：

```bash
.venv/bin/desk-agent \
  --env-file .runtime/redmine.env \
  resume --ticket 1 --session vpn-review --message '继续上次的调查。'

.venv/bin/desk-agent --env-file .runtime/redmine.env \
  status --ticket 1 --session vpn-review
```

默认授权为只读。`--allow-notes` 授权备注；`--allow-status ID` 授权具体状态转换；关闭还需 `--allow-close`。恢复时沿用持久化的任务合同。授权变更通过新会话显式授予。

恢复输入会先进入新一轮模型请求。旧的排队调用以 `superseded_by_user_input` 结束，已持久化的完成结果继续保留。例如：

```bash
.venv/bin/desk-agent --env-file .runtime/redmine.env \
  resume --ticket 1 --session vpn-review \
  --message '停止写入，只读取当前工单和交接记录；对先前操作执行只读回执核对。'
```

Agent 的 `reconcile_ticket_operations` 工具只回读 Redmine，将当前可核验的回执补入原生 Checkpoint。它支持操作账本已保存、工具 `Command` 尚未提交时的恢复。普通 `ticket` 命令读取工单，`status` 命令读取保存的交接记录。

CLI 的 `start`、`resume` 和 demo 使用持久化 `memory.sqlite` 保存写入意图与紧凑回执。提交前记录操作哈希、源版本、状态和 journal 边界，作用域包含服务、项目、API 用户、工单及会话。`operation_uncertain` 表示提交仍待核对；同会话的新 tool call id 或新 payload 继续受此约束。回读确认后解除阻塞，成功回执可重放。已确认动作的后续状态变化返回 `action_superseded`，历史证据变化返回 `action_evidence_changed`，完成记录继续保留。API 密钥独立于账本存储。

直接使用 `RedmineClient` 时，写入需要显式注入 Store；生产接线使用文件型 `SqliteStore`，测试使用 `InMemoryStore`，默认 client 提供只读访问。

`--components` 开启 B 组的原生 Skills、版本化政策检索和长期偏好查询，需同时配置 `DESK_POLICY_CATALOG` 与 `DESK_KNOWLEDGE_SCOPE`。明确确认的偏好通过以下入口保存，作用范围为当前服务、项目、API 用户和工单：

```bash
.venv/bin/desk-agent --env-file .runtime/redmine.env \
  remember --ticket 1 --text '调查进展使用中文记录。'
```

## 开发评测

固定基准为 `salesforce-misc/crm-agent-bench`，源码：

```text
fc9575cf9bda085aa1f66d920fea944588005964
```

21 题采用报告中的固定划分：9 题开发集、12 题留出集，种子 `20261105`。三题校准属于开发集；留出入口要求提供冻结协议。

```bash
.venv/bin/desk-agent bench select
.venv/bin/desk-agent bench smoke --output artifacts/my-wiring-check

.venv/bin/desk-agent bench run --split calibration --groups A B \
  --max-turns 15 --max-calls 300 --output artifacts/my-calibration

.venv/bin/desk-agent bench run --split development --groups A B \
  --max-turns 15 --max-calls 600 --output artifacts/my-development
```

A 使用原生 Harness、完整必要政策和业务工具；B 增加现成 Skills、BM25 和 LangMem 查询。当前共同政策配置为 `shared-policy-and-public-workflows`，包含固定源码中的共享 ITSM policy 与公开工单、删除、业务时间 SOP。两组使用相同政策原文、模型、业务环境、原生 Grader 与调用限制，每个任务有独立的 Checkpoint 和 Memory namespace。政策来源及内容指纹保存在 `config.json`、`policies.json`。

结果目录保存配置、源码快照、逐题状态、工具事件、原生验收结果及分角色模型用量。用户模拟器使用与 Agent 相同的模型；选定任务的语义 Judge 调用数为零。费用根据 `.env.example` 中的单价字段计算，单价留空时费用为 `null`。

并行运行 A/B 时，分别使用独立输出目录，完成后汇总：

```bash
.venv/bin/python scripts/report.py \
  --run artifacts/development-A --run artifacts/development-B \
  --output artifacts/development-comparison.json
```

### 冻结留出

在同一模型环境和运行时中创建新冻结目录，再执行与冻结参数一致的留出评测：

```bash
.venv/bin/desk-agent bench freeze \
  --groups A B --max-calls 600 --max-turns 15 \
  --output artifacts/my-holdout-freeze

.venv/bin/desk-agent bench run \
  --split holdout --groups A B --max-calls 600 --max-turns 15 \
  --freeze artifacts/my-holdout-freeze/freeze.json \
  --output artifacts/my-holdout
```

`freeze` 以零模型调用生成 `freeze.json` 和源码归档，记录模型参数、源码、依赖、政策、固定任务划分、组别顺序和执行上限。执行前与 episode 边界校验运行时；同一冻结目录通过 `execution.json` 登记一次尝试，中断和部分完成均保留原始记录。已完成留出的 [配对汇总](results/holdout.json)、[运行结果](results/runs/holdout-luna-20261105/summary.json) 和 [冻结协议](results/runs/holdout-freeze-luna-20261105/freeze.json) 均有独立归档，详细用量见 [验证结果](docs/results.md)。

详细结果见 [验证结果](docs/results.md)，评测口径见 [评测协议](docs/evaluation.md)，公开的原始记录见 [results](results/README.md)。

## MCP

只读 Redmine MCP 服务使用官方 Python SDK：

```bash
.venv/bin/desk-agent --env-file .runtime/redmine.env mcp-serve
```

可供客户端调用的工具为 `service_info`、`read_ticket`。MCP stdio 与 `MultiServerMCPClient` 接线由自动化测试覆盖。常用客户端配置：

```json
{
  "mcpServers": {
    "servicedesk": {
      "command": "/absolute/path/to/servicedesk-agent/.venv/bin/desk-agent",
      "args": [
        "--env-file",
        "/absolute/path/to/servicedesk-agent/.runtime/redmine.env",
        "mcp-serve"
      ]
    }
  }
}
```

客户端配置中的路径使用项目实际安装位置。

## 验证与维护

```bash
.venv/bin/pytest
.venv/bin/ruff check src scripts tests
.venv/bin/ruff format --check src scripts tests
.tools/uv pip check
.venv/bin/python scripts/redmine.py status
.venv/bin/python scripts/cpa.py status
```

`scripts/redmine.py rotate-keys` 轮换隔离测试实例的 API 密钥与登录密码。`scripts/redmine.py stop` 停止容器并保留数据；`scripts/cpa.py stop` 关闭本项目的 SSH 隧道。

`.runtime/`、`.env*`、`artifacts/`、虚拟环境和第三方源码均由 Git 忽略。业务轨迹包含工单内容，应按业务数据管理。

## 实现

执行循环、压缩、文件上下文、Skills 加载和持久化复用 [DeepAgents 原生接口](https://docs.langchain.com/oss/python/deepagents/customization)。工单接口使用 [Redmine Issues REST API](https://www.redmine.org/projects/redmine/wiki/Rest_Issues)。主要业务代码和部署约束见 [架构说明](docs/architecture.md)。

当前交付 A/B 两组，生命周期策略 C 保持关闭。C 的启用条件是出现可复现的业务证据错误，并在独立回归与固定任务中体现收益。

第三方任务、轨迹和依赖的来源见 [第三方声明](THIRD_PARTY_NOTICES.md)。
