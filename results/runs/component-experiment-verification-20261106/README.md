# 实验代码与材料校验

Python 3.12 环境完成项目测试及实验数据校验：596 passed、1 skipped。跳过项为
`tests/test_mcp.py:132`，依赖本机实际 Redmine 服务。上游 `trustcall` 的 LangGraph
弃用提示保留于测试输出。

```bash
.venv/bin/pytest -ra tests \
  benchmarks/business-reliability/test_package_raw_state.py \
  benchmarks/preference-memory/test_analyze.py \
  --junitxml=artifacts/component-experiments-tests.xml
```

完整记录：[full-suite.xml](full-suite.xml)。

实验脚本、分析器及定向测试通过 Ruff 检查。政策组八次执行的精确备注、HTTP 写入、journal
和回执关联均通过离线核算，16 份原始 SQLite 完整性检查通过。Memory 正式组与三轮校准的
模型账本、业务评分、源码及场景指纹分别复算，72 份 SQLite 已从归档恢复核验。
故障实验的归档恢复结果与正式 summary、CSV 逐字节一致。

业务终态和真实模型表现按各自冻结协议评分；工程测试计数在本页单独记录。
实验入口：[组件结果](../../../docs/component_experiments.md)。
