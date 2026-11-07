# 政策适用性扩展实验

在 48 份合成政策、76 条查询上，有效期过滤将 64 条可回答查询的首位命中从 **18/64 提升到 62/64**，Top-3 命中从 **62/64 提升到 64/64**。新旧版本的适用性得到改善，语义排序仍保留两条首位错误和一条无关查询误召回。

## 语料与结果

语料包含八个新增政策主题、两个业务范围、每个范围三个相邻版本，共 16 个政策身份、48 份文档。查询覆盖版本切换前一秒、切换时刻、当前版本、未来版本、尚未发布、未注册业务范围及无关问题。64 条可回答查询共享政策主题和日期变体；独立主题数为 8。

两组使用相同的生产 `PolicyCatalog`、Haystack BM25、业务范围、语料和 Top-3 参数。对照仅去掉搜索时的有效期过滤。语料与评分要求在执行前冻结；本实验包含 152 次检索，模型请求为 0。

| 指标 | 仅业务范围过滤 | 范围与有效期过滤 |
| --- | ---: | ---: |
| 有效版本 Hit@1 | 18/64（28.1%） | 62/64（96.9%） |
| 有效版本 Hit@3 | 62/64（96.9%） | 64/64（100%） |
| 返回中的时效不适用项 | 142/207 | 0/135 |
| 无适用政策时正确返回空结果 | 4/8 | 8/8 |
| 无关查询正确返回空结果 | 3/4 | 3/4 |
| 业务范围外结果 | 0 | 0 |

两条首位错误来自两个范围下的 `approve urgent security patching`：BM25 将证书续期排在安全补丁之前，正确政策位于第二位。无关查询 `parking garage bicycle space` 召回存储扩容，反映关键词 `space` 的歧义。所有原始结果和错误保留在同一分母。

该实验测量版本化检索。真实 Agent 的政策理解、业务写入与成功率由独立任务实验衡量。

## 复算

- [冻结语料](cases.json)
- [协议与源码身份](../../results/runs/policy-applicability-20261107-001/protocol.json)
- [逐查询返回](../../results/runs/policy-applicability-20261107-001/retrieval.json)
- [分组及分时点汇总](../../results/runs/policy-applicability-20261107-001/summary.json)
- [冻结源码包](../../results/runs/policy-applicability-20261107-001/source.tar.gz)
- [独立复算记录](../../results/runs/policy-applicability-20261107-001/audit.json)

生产模块以源码包和逐文件哈希固定；基础提交为 `d02672075ae4e0b03ccce5178c8cf487a59660df`。严格复现时，在该提交的独立检出中解开 `source.tar.gz`，按包内 `uv.lock` 安装 components 依赖。新的运行使用独立输出目录：

```bash
PYTHONPATH=src python scripts/benchmark_policy_applicability.py \
  --output results/runs/policy-applicability-reproduce
python benchmarks/policy-applicability-20261107/audit.py \
  results/runs/policy-applicability-20261107-001 --output /tmp/policy-audit.json
```

离线审计核对冻结源码、完整查询集合、逐条件分母和命中／适用性计数。仅有公开结果包时也可直接执行审计。
