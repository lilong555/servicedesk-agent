# 政策时效与任务恢复实验

实验复用生产 DeepAgents 图、会话 Checkpointer、LangMem/SQLite 组件及 RedmineClient，
以真实 `gpt-6-luna` 模型处理合成 HTTP 业务服务上的工单。检索层与任务层分别评分。

## 数据与对照

`cases.json` 定义 24 份合成政策：八个带业务范围的政策身份，每个身份有三个有效期相邻的版本。
十二条查询包含九条可回答查询、两条无适用文档查询和一条无关查询；历史查询按指定时点评分。

两组共享 BM25 索引、业务范围、工具 Schema、全文读取接口、模型设置与任务权限。
唯一变量为检索时的有效期过滤。对照组保留范围过滤，两组均允许显式读取历史版本。
两组沿用生产 Skill 中“检索返回当前版本”的描述；无时间过滤组的实际候选包含历史与未来版本。
共同工具说明要求检查有效期，这项提示与返回值的差异随冻结输入保留。

Agent 场景包含“新旧版本并存”及“恢复时跨越政策生效时点”。每个场景、每种条件重复两次，
共八次执行。恢复场景在 11 月 4 日调查，在 11 月 6 日继续同一原生 thread；两版政策预先存在，
通过推进场景时钟切换其适用状态。

任务输入指定政策身份，授权一条备注，并要求附政策版本与处理代码。正确版本及代码由政策证据提供。
评分检查最终 journal、校验回执、工单状态，以及准备阶段的零写入。

## 实验结果

来源：[冻结协议](../../results/runs/policy-lifecycle-20261106/protocol.json)、
[逐查询结果](../../results/runs/policy-lifecycle-20261106/retrieval.json)、
[Agent 逐次记录](../../results/runs/policy-lifecycle-20261106/episodes.json)。

| 指标 | 仅范围过滤 | 范围与有效期过滤 |
| --- | ---: | ---: |
| 相关且适用的 Hit@1 | 1/9 | 9/9 |
| 相关且适用的 Hit@3 | 9/9 | 9/9 |
| 返回文档中的时效不适用项 | 21/30 | 0/18 |
| 无适用文档时正确返回空结果 | 1/2 | 2/2 |
| Agent 任务完成 | 4/4 | 4/4 |
| Agent 模型请求 | 47 | 45 |
| Agent 逻辑输入 Token | 251,170 | 239,702 |
| Agent 输出 Token | 2,173 | 2,268 |
| Agent 缓存输入 Token | 192,640 | 187,136 |
| 用量未知请求 | 0 | 0 |

八次 Agent 执行均调用了政策检索与全文读取。四次恢复执行均在准备阶段读取 v1，
在推进时钟后读取 v2。该小型开发集呈现检索排序改善与任务完成数持平。

检索命中率以九条可回答查询为分母，空结果及无关查询单列。八次执行覆盖两个场景身份及重复条件。
逻辑输入包含缓存输入；费用保持未定价，延迟作为原始记录保留。

## 复现

在已安装可选组件的项目环境中执行：

```bash
uv sync --frozen --extra components
.venv/bin/pytest tests/test_policy_lifecycle_benchmark.py
.venv/bin/python scripts/benchmark_policy_lifecycle.py \
  --output results/runs/policy-lifecycle-retrieval-reproduce
.venv/bin/python scripts/benchmark_policy_lifecycle.py \
  --live --repeats 2 --env-file .runtime/cpa.env \
  --output results/runs/policy-lifecycle-live-reproduce
```

每次运行使用新输出目录。模型调用前保存协议、源码哈希、依赖版本、条件顺序及评分定义；
原始轨迹、HTTP 操作、业务终态及逐请求用量全部保留，失败进入登记分母。

执行源码快照为 [source.tar.gz](../../results/runs/policy-lifecycle-20261106/source.tar.gz)，
SHA-256：`4b7713902e86a61d05f9c53a705e7b59e66be5e69f1033cca5debd56fee7d1e9`。
该定向场景集使用独立评分，既有 CRM-Agent-Bench 任务及原生 Grader 保持冻结。

原始结果支持零模型请求的离线复算：

```bash
python3 benchmarks/policy-lifecycle/analyze.py \
  results/runs/policy-lifecycle-20261106 \
  --output artifacts/policy-offline-analysis.json
```

分析器复核全部查询、调度、逐请求用量及汇总，并严格比对备注全文、HTTP 写入、
操作标记、journal 与经过验证的回执。八次执行均通过
[严格复核](../../results/runs/policy-lifecycle-20261106/offline-analysis.json)；
原始冻结评分保持原样。

## 源码与状态

基础提交为 `49850cd965ab19789abfa45fc654dc60acbcce45`。`source.tar.gz` 保存运行脚本、
场景和生产 Python 模块；依赖锁与 Skill 资产来自同一提交，并保存于
[dependency-assets.tar.gz](../../results/runs/policy-lifecycle-20261106/dependency-assets.tar.gz)，
SHA-256：`ec8625fc27abb4f29994041f6f4cb73b61297162f9bea38c1cb5c5c5870b70b5`。

[raw-state.tar.gz](../../results/runs/policy-lifecycle-20261106/raw-state.tar.gz)
保留八次执行的合成 Store、Checkpoint 与原生轨迹，包含 16 份 SQLite 数据库。
SHA-256：`d83954e096180ffd0baed887b80a322f1504b758f5a527e21020d7fdf6d160d7`。
逐文件哈希与数据库完整性结果见
[state-audit.json](../../results/runs/policy-lifecycle-20261106/state-audit.json)。
仅有归档的公开检出也可执行以下只读校验：

```bash
python3 benchmarks/policy-lifecycle/audit_state.py \
  results/runs/policy-lifecycle-20261106 \
  --output artifacts/policy-state-audit.json
```
