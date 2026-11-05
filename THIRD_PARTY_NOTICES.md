# 第三方声明

## CRM-Agent-Bench

任务选择清单、测试调用，以及 `results/runs/` 中的基准政策、合成业务数据和衍生轨迹来自 [salesforce-misc/crm-agent-bench](https://github.com/salesforce-misc/crm-agent-bench)，固定源码为 `fc9575cf9bda085aa1f66d920fea944588005964`。

原项目采用 Apache License 2.0。完整 [LICENSE.txt](third_party/crm-agent-bench/LICENSE.txt) 与 [NOTICE.txt](third_party/crm-agent-bench/NOTICE.txt) 随本仓库保留。运行时源码由 `scripts/bootstrap.sh` 从上游检出到 `vendor/crm-agent-bench`。

## 运行依赖

Python 依赖及其完整版本由 `pyproject.toml` 与 `uv.lock` 记录，各依赖保留其自身许可。DeepAgents、LangChain、LangGraph、LangMem、Haystack 和 MCP 通过原有包接口接入。

Redmine 使用官方容器镜像独立运行，应用通过 REST API 访问。其源码与许可由 [Redmine 项目](https://www.redmine.org/) 提供。
