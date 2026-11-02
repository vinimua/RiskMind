# RAG 真实 Embedding 对比结果：2026-11-02

## 执行结论

本轮端到端执行成功，原始报告状态为 `SUCCEEDED`，但这只表示实验执行成功，不代表检索质量已通过生产验收。

使用真实 SiliconFlow `BAAI/bge-m3` 1024 维向量、真实 PostgreSQL 与 Milvus，完成两套策略的 staging、rebuild、验收、显式激活、检索和上下文解析。没有使用固定测试向量，没有加入 BM25、Reranker 或 Query Rewrite。

## 环境与范围

- 数据集：`riskmind-synthetic-reports-v2`，两份 synthetic 报告、六个固定问题；实际 dataset_id 和摘要以原始 JSON 为准。
- 每个问题、每种策略重复三次，合计 36 条记录；相同 query、filters、top_k=5。
- Baseline：65 个 Child；Parent-Child：65 个 Child、38 个 Parent。
- PostgreSQL：新建隔离容器，16-alpine，数据库 `knowledge_benchmark`，仅本机端口 55439；迁移 0001→0002→0003 成功。
- Milvus：独立 etcd、MinIO 与 Milvus 2.6.24 评测栈，本机端口 19539。默认监控端口冲突后采用独立端口，不修改其他应用服务。
- 两个物理集合分别为 `riskmind_benchmark_20261102_baseline` 与 `riskmind_benchmark_20261102_parent_child`，各自 route_version=1。
- 本轮不启用答案生成/LLM judge，generation_status=NOT_RUN。
- 未指定下游生成模型 tokenizer，context_tokens=null，只报告字符数，不把字符冒充 token。

## 汇总结果

以下为各策略 18 次运行的宏平均或报告延迟分位数。

| 指标 | Baseline | Parent-Child |
|---|---:|---:|
| Child 标准引用召回率 | 43.06% | 43.06% |
| 最终 Context 标准引用召回率 | 43.06% | 54.17% |
| Context precision | 16.67% | 22.22% |
| 字符长度加权引用覆盖率 | 47.14% | 55.35% |
| 完整覆盖全部引用的问题比例 | 16.67% | 33.33% |
| Child Precision@5 | 16.67% | 16.67% |
| Child Recall@5 | 47.22% | 47.22% |
| Hit@5 | 83.33% | 83.33% |
| MRR@5 | 0.5889 | 0.5889 |
| nDCG@5 | 0.3982 | 0.3982 |
| 平均 Context 字符数 | 863.17 | 1259.00 |
| 平均 Context 数量 | 5.00 | 4.67 |
| 平均单请求 Context/Child 字符比例 | 1.000 | 1.593 |
| 标准引用重复匹配比例 | 0 | 0 |
| 端到端 p50 | 151.16 ms | 171.05 ms |
| 端到端 p95 | 2453.69 ms | 255.59 ms |

最终引用召回提高 11.11 个百分点；平均上下文字符数增加约 45.9%。平均逐请求扩展比例 1.593 与“平均 Context 字符数之比”分母口径不同，不应混用。

Baseline 的最慢记录是首轮首个根因问题，耗时 2453.69 ms，显著影响只有 18 个样本时的 nearest-rank p95。报告保留该冷启动样本，没有因数值不好看而剔除。不能由这组 p95 推断 Parent-Child 天然快一个数量级。

## 按问题分析

三轮中，每个问题、每个策略返回的 Child ID 序列一致。下面列出首轮结果；重复三次不等于增加了独立问题样本数。

| 问题 | Baseline 引用召回 | Parent-Child 引用召回 | Context 字符数：Baseline / PC |
|---|---:|---:|---:|
| incident-root-cause：根因机制与复算材料 | 25% | 25% | 1348 / 1525 |
| incident-maturity：未成熟样本规模与影响 | 0% | 0% | 878 / 1326 |
| incident-action：回滚与后续监控措施 | 33.33% | 100% | 675 / 1317 |
| incident-validation：成熟样本 KS/AUC 复算 | 100% | 100% | 789 / 885 |
| development-label：坏样本与训练准入 | 50% | 50% | 460 / 1176 |
| development-selection：模型与特征数量 | 50% | 50% | 1029 / 1325 |

### 可以支持的判断

1. 完整工程链路已在真实存储和真实 Embedding 上跑通。
2. 这份语料上，两种策略 Child 指标相同；Parent 展开主要改善行动建议问题的上下文覆盖。
3. Parent 展开伴随更长上下文，没有普遍补齐所有问题材料。
4. 三轮结果稳定，但语料和问题规模很小，不能把它解释为跨场景泛化能力。

### 需要正视的缺口

- 根因问题仅覆盖四条标准引用之一，仍缺低估机制及 KS/AUC 表格材料，不能声称完整根因上下文已召回。
- 标签成熟度问题两种策略都没有完整覆盖标准引用。0% 指当前精确引用标注下的覆盖，不等于返回内容完全没有语义关系。
- 模型选择问题缺少特征数量引用；标签定义问题缺少坏样本定义引用。
- 上下文精确率较低，返回的五个 Child/若干 Parent 中存在未覆盖已标注引用的内容。但标注并非穷尽语义相关性，不应把全部未标注结果直接认定为无用。

## 指标边界

引用匹配要求文档 ID、版本一致，且完整原文连续出现；不跨 Child/Context 拼接，不给部分句子匹配分数。

Context precision 按“包含至少一条标准引用的 Context 条数”计，不是逐 token 精确率。冗余指标仅检查标准引用重复，不代表全部文本语义冗余为零。

本轮未评价答案正确性、忠实度或引用质量，因为尚未指定本轮使用的答案模型和 judge 模型。真实 Embedding 成功不等于生成侧效果已验证。

## 下一步

先复核缺失引用对应的 Child 排名、正文和章节，确认是候选未进 Top-5、切分边界还是正文缺少背景造成的影响。再补充真实长报告与独立问题标注，在固定数据集和同一基线上评估后续 BM25/Reranker；本轮不修改 sealed 算法。

生成侧评估需要显式指定答案模型与评审模型；token 成本需要固定对应的 tokenizer。现有评测代码已提供相应入口。

## 产物与可复查环境

- [完整原始结果 JSON](knowledge-benchmark-real-20261102.json)：36 条原始命中、分数、路由、上下文、指标与计时。
- [评测说明](runbooks/knowledge-evaluation.md)
- [Baseline/Parent-Child 运行说明](runbooks/knowledge-benchmark.md)

评测 PG、Milvus 集合与独立容器保留用于复查，未修改生产路由。环境名称均带 `riskmind-bench` 和日期；复查结束后可仅清理这些独立资源。
