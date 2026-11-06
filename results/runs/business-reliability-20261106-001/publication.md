# 公开数据与状态恢复

正式实验的原始 SQLite 状态保存在 [raw-state.tar.gz](raw-state.tar.gz)。归档包含 413 个数据库及 2 个 SQLite 旁文件，共 415 个普通文件；压缩大小 497,902 字节，原始大小 10,838,016 字节。包内路径相对本 run 根目录，目录仅为 `main/`、`deterministic/`、`revocation/`。

SHA-256：

```text
fd8a956917bd845c2f1cf32dc2c65a8f96b586538ffa10555b9e0cd68ce107f0
```

[raw-state.sha256](raw-state.sha256) 提供校验格式；[raw-state.manifest.json](raw-state.manifest.json) 提供每个成员的相对路径、大小、SHA-256、公开输入文件清单、包装脚本指纹及内容检查结果。原始 SQLite、JSON、trace 和冻结源码归档均保留。

## 恢复与重算

工作目录为仓库根目录，使用已安装项目依赖的 `.venv`：

```bash
(cd results/runs/business-reliability-20261106-001 && sha256sum -c raw-state.sha256)

PYTHONDONTWRITEBYTECODE=1 LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false \
  .venv/bin/python benchmarks/business-reliability/package_raw_state.py verify \
  --destination results/runs/business-reliability-20261106-restore-check

PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  scripts/benchmark_business_reliability.py summarize \
  results/runs/business-reliability-20261106-restore-check
```

每次复核使用新的目标目录。`verify` 从正式 run 复制清单中的 1,266 个公开输入文件，校验归档和成员路径，恢复 SQLite，再调用生产实验脚本的 `summarize`。目标目录同时保存本次 `raw-state.restore-verification.json`。恢复操作使用普通文件写入，提前验证文件类型、重复成员、路径、大小和哈希。

已执行的独立临时目录为 `results/runs/business-reliability-20261106-raw-state-restore-tmp/`。[实际复核记录](raw-state.restore-verification.json) 确认：

- `summarize` 退出码为 0。
- summary 与 operations.csv 分别与正式结果逐字节一致。
- 415 个恢复状态文件的 SHA-256 均与归档清单一致。
- 正式 run 原有文件的 SHA-256 保持一致。
- 原实验脚本与 `freeze.json` 的冻结指纹一致。

原始实测值仍为两组各完成 100/100，基线及只读对账分别产生 50/0 条重复备注、150/100 次 HTTP 写入；撤权成功后 HTTP 写准入为 0。

## 内容检查

包装前检查了 205 份 case 的夹具、业务地址、项目、身份、工单与备注模板；413 个数据库均通过 SQLite `quick_check`。数据库和旁文件的原始字节扫描覆盖常见模型服务密钥、GitHub token、AWS access key、私钥、Bearer token 和凭证赋值字段，命中数为 0。

SQLite 中的业务服务地址仅为 `https://business-reliability.invalid`，业务内容来自固定合成夹具。包内同时保留本地路径、进程 ID 和时间戳等运行元数据；HTTP 认证头未写入原始请求轨迹。完整检查结果位于 manifest 的 `synthetic_content_audit`。包装安全回归为 13 passed，Ruff 通过。

## 公共提交范围

[publication-files.txt](publication-files.txt) 是逐文件的仓库相对路径清单，可直接作为主线程的 Git pathspec：

```bash
git add --pathspec-from-file=results/runs/business-reliability-20261106-001/publication-files.txt
```

清单包含以下精确范围：

- `scripts/benchmark_business_reliability.py`
- `tests/test_business_reliability_benchmark.py`
- `benchmarks/business-reliability/protocol.json`
- `benchmarks/business-reliability/redmine.json`
- `benchmarks/business-reliability/package_raw_state.py`
- `benchmarks/business-reliability/test_package_raw_state.py`
- 正式 `business-reliability-20261106-001/` 的公开普通文件，包括状态归档、manifest、SHA、复核记录及本说明。
- `business-reliability-20261106-verification/` 顶层的日志、XML、JSON 与验证说明。

原始 SQLite 通过归档交付。运行锁、`verification/` 下的测试临时目录，以及独立解包临时目录均留在本地。路径清单覆盖本实验自身文件，其他实验、生产核心和现有脏改动由主线程分别处理。

包装回归命令：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider \
  benchmarks/business-reliability/test_package_raw_state.py \
  --basetemp=results/runs/business-reliability-20261106-verification/packaging-retest-tmp
```
