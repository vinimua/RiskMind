# Stage 6E — Baseline vs Parent-Child benchmark

本工具仅比较已封板的 Baseline Dense + Child Context 与 Parent-Child Dense
Child + Parent Context。不添加 BM25、Reranker、Query Rewrite，不改 serving schema。

## 语料和标注

`backend/tests/fixtures/knowledge_benchmark_v1.json` 固定两份仓库 synthetic report
的完整 KnowledgeDocument，以及六个问题、原文引用和稳定 quote ID。
运行 `python scripts/_prepare_benchmark_dataset.py` 可从原 fixture 重建；修改语料、
问题或标注后必须递增 dataset_id。报告记录规范 JSON 的 SHA256。

这些标注是初始手工选择，需要领域 review，相关片段不是穷尽标注。
不得把 exact quote availability 称为正式 Evidence Recall，也不代表回答正确率。
`incident-root-cause` 同时要求根因、低估机制、KS/AUC 复算片段，检查完整根因描述
所需材料是否齐全；本阶段不生成答案，不使用 LLM judge。
短 section 居多的小语料不足以证明长报告上的 Parent-Child 收益。

## 准备独立环境

使用专用 PostgreSQL 数据库和 Milvus physical collections，禁止使用生产路由。
沿用现有 API 完成如下准备（本工具不自动授权、写入或切换路由）：

1. 在专用库执行 `alembic upgrade head`。
2. 读取 dataset.documents，对每个 profile 用对应 strategy.build(document)，
   在事务内调用 repository.stage_prepared。
3. 每个 profile 分别 rebuild_collection 到新的 REBUILD physical collection；
   使用相同 BAAI/bge-m3 模型和相同来源，不能用固定向量代替真实 embedding。
4. validate_serving_collection，再由有权限的操作者通过
   activate_serving_collection 显式授权激活各自 route。
5. 运行期间停止 staging / route activation，确保两个 serving build 都覆盖完整语料。

设置 RISK_DATABASE_URL、MILVUS_URI、SILICONFLOW_API_KEY；可选
SILICONFLOW_BASE_URL。运行前后工具核对 PG canonical 文档、所有 Child payload、
acceptance digest 以及 route。两份 profile 不得混入其他语料或使用旧 serving build。
Parent/Link 继续依赖 sealed application immutability，未新增 digest 绑定。

```powershell
python scripts/benchmark_knowledge.py `
  --dataset backend/tests/fixtures/knowledge_benchmark_v1.json `
  --output benchmark-result.json --repeats 3
```

从仓库根目录运行，需已安装 backend 的 database / knowledge 依赖。
可选安装 `tokenizers` 并传 `--tokenizer-json <固定的下游生成模型tokenizer.json>`。
报告保存 tokenizer 文件 SHA256；不自动下载或选择模型。没有 tokenizer 时
context_tokens 为 null，不能用字符数冒充 token。token 数仅计算各 context.content
独立编码的总和，不含提示词、标题、分隔符和生成回答。

## 指标定义

所有匹配均要求 document_id + version 一致，且完整 gold quote 连续出现在文本中。
不把断开的半句、跨 Context 拼接、错误来源的相同文字计为覆盖。

| 指标 | 定义 |
|---|---|
| child_relevant_quote_recall | Child 文本覆盖的 gold quote 数 / gold quote 总数 |
| context_relevant_quote_recall | 最终 Context 覆盖的 gold quote 数 / gold quote 总数 |
| context_precision | 含至少一个完整 gold quote 的 Context 数 / Context 数 |
| context_coverage | 完整覆盖 quote 的字符长度之和 / 全部 quote 字符长度之和 |
| complete_relevant_context | 该问题所有 gold quote 是否均被 Context 覆盖 |
| context_tokens | 可选固定 tokenizer 的 content token 总数 |
| context_to_child_character_ratio | Context 总字符数 / 检索 Child 总字符数；Parent 扩展成本代理 |
| redundant_gold_match_ratio | 重复出现的 quote-context 匹配数 / 总 quote-context 匹配数 |
| retrieval_ms / resolution_ms / total_ms | 实际 pipeline 耗时，包含 embedding / 网络 / PG，不含评分 |

空结果 recall/precision/coverage 为零，扩展比例为 null。
冗余仅衡量 gold quote 跨 Context 重复，不是全部文本语义冗余；precision 是 Context
级别，不是字符或 token 级 precision。coverage 是长度加权的完整引用覆盖，
不是部分字符匹配。避免把这些代理指标解释得超出定义。

每个问题两种 profile 共用相同 query、filter、top_k，重复运行交替调用顺序。
检索各自使用原始 route snapshot；工具不在请求内部重新路由。
报告保留每次原始 Child/Context、supporting_hits、score、route_version 和耗时，
汇总 macro mean 及端到端 p50/p95（nearest-rank）。没有独立 warmup，首次冷启动
样本保留；少量请求的 p95 仅供观察。固定 top_k 比较不是固定 token budget 比较。
应 review 每个问题的缺失 quote 和噪声，而不是仅看总平均数。

真实依赖、来源校验或任何请求失败，整次运行退出码 2，输出 INCONCLUSIVE，
不发布部分结果或虚构 A/B 优势。报告只保留异常类型，不包含可能带凭据的依赖异常消息。
输出包含完整 synthetic 文本；替换为真实语料前需遵循数据访问和报告保管要求。

## 当前结论与保留风险

当前环境未配置 SILICONFLOW_API_KEY，故本轮真实效果评测为 INCONCLUSIVE。
确定性 tests 中固定向量只验证执行与评分链路，不是语义性能实验。
需要真实 embedding、独立已激活 serving collections 和固定 tokenizer 后才能回答
Parent-Child 提升多少、token 和延迟成本多少。

保留原 P2：serving 全量验收持锁成本、Milvus preflight→bind TOCTOU、legacy
Baseline adoption、COSINE score 范围、Parent/Link 未绑定 acceptance digest。
新增评测限制：语料规模小且标注未做独立领域验收，生产结论需扩展问题集和真实长报告。
