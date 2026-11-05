# 工单交接演示

使用现有命令行和 Redmine 界面，查看同一张工单从调查、外部状态变化到跨进程恢复的完整过程。交接演示启用 B 的版本化政策能力；固定 CRM 评测默认使用 A。

## 运行

按 [README](../README.md#启动)安装依赖、启动隔离 Redmine 并配置模型。在浏览器登录 [Redmine](http://localhost:3080)，然后运行：

```bash
.venv/bin/desk-agent --env-file .runtime/cpa.env \
  demo --live --output artifacts/handoff-example
```

每次使用新的输出目录。采用其他模型服务时，使用对应环境变量或环境文件。确定性接线验证省略 `--live`，仍通过真实 Redmine API 执行。

命令创建独立测试工单并完成四个阶段：

1. 创建 VPN 调查工单，明确保持开放的用户约束。
2. 第一个进程读取工单与政策，添加调查备注，保存待办后退出。
3. 管理员更换负责人，政策目录发布 v2。
4. 第二个进程恢复原生 Checkpoint，依据当前负责人和政策继续处理，回读日志核验结果。

输出中的 `ticket_url` 指向本次工单；`passed` 与 `checks` 给出验收结果。可追加 `--stage-delay 5` 在前三个阶段结束后各等待五秒，便于界面核对。

## 查看证据

| 文件 | 内容 |
| --- | --- |
| `summary.json` | 业务验收、工单地址与两个进程的轨迹位置 |
| `initial.json`、`resume.json` | 各阶段结果、持久合同、待办与回执 |
| `stages/01-created.json` | 创建后的真实工单快照 |
| `stages/02-initial-completed.json` | 首次调查备注后的快照 |
| `stages/03-external-changes.json` | 负责人和政策变化后的快照 |
| `stages/04-resume-completed.json` | 恢复处理后的快照 |
| `run.json`、`source.tar.gz` | 运行配置、源码指纹与可复查源码 |

读取保存的交接记录，使用输出中的工单编号：

```bash
read -r -p '本次工单编号：' TICKET
DESK_STATE_DIR="$PWD/artifacts/handoff-example/state" \
  .venv/bin/desk-agent --env-file .runtime/redmine.env \
  status --ticket "$TICKET" --session vpn-handoff
```

`status` 使用零模型请求，返回授权、约束、待办和精简回执。回执中的 `journal_id`、来源 URI 与业务版本可同 Redmine 历史核对。

## 停止与撤销

将最新停止要求传入 `resume --message`，会在下一次模型决策前取消旧排队调用。持久撤销当前会话的全部写入权限使用：

```bash
DESK_STATE_DIR="$PWD/artifacts/handoff-example/state" \
  .venv/bin/desk-agent --env-file .runtime/redmine.env \
  revoke --ticket "$TICKET" --session vpn-handoff
```

撤销后原有任务记录继续保留，后续恢复使用只读合同。备注和状态写入由业务工具检查授权，已发送请求通过只读对账确认。

独立验收入口与运行结果见 [README](../README.md#权限撤销验收)、[结果卡](result-card.md)及[完整结果](results.md)。归档的 [真实交接](../results/runs/redmine-live-reliable-20261105/summary.json)和[停止恢复](../results/runs/cancellation-live-20261105/summary.json)包含对应工单、逐项检查与实际模型用量。
