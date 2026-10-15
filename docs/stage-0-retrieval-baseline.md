# Stage 0：检索基础设施技术基线

本阶段是可删除的技术探针，不提供生产 `KnowledgeRetriever`、领域契约或工作流节点。

## 候选基线

- Milvus Server：`2.6.24`
- PyMilvus：`2.6.17`
- 部署：Standalone / Docker Compose
- Embedding Provider：SiliconFlow OpenAI-compatible API
- Embedding Model：`BAAI/bge-m3`（标准免费版）
- Dense Vector：固定 `1024` 维，`COSINE`
- 物理 Collection：`riskmind_knowledge_v001`、`riskmind_knowledge_v002`
- 活跃 Alias：`riskmind_knowledge_active`

最初的 `embedding-3 / 1024 vs 2048` 实验已被后续选型取代。`BAAI/bge-m3`
返回固定 1024 维，SiliconFlow API 不支持为该模型指定 2048 维。因此本阶段验证真实返回维度、
领域 Top-K 和 1024 维 Collection，不伪造 2048 对照组。若未来更换 Embedding 模型，必须新建
Collection、重新向量化并切换 Alias。

## 候选 Collection Schema

| 字段 | 类型 | 用途 |
|---|---|---|
| `chunk_id` | VARCHAR, PK | 稳定片段标识 |
| `document_id` | VARCHAR | 回查 PostgreSQL 权威文档 |
| `document_type` | VARCHAR | 高频过滤 |
| `document_version` | VARCHAR | 业务追溯 |
| `title` | VARCHAR | 命中展示 |
| `section_path` | VARCHAR | 章节定位 |
| `content` | VARCHAR + Analyzer | 检索正文 |
| `model_id` | VARCHAR | 高频过滤 |
| `case_id` | VARCHAR | 精确案例过滤/返回 |
| `dense_vector` | FLOAT_VECTOR(1024) | Dense / COSINE |
| `sparse_vector` | SPARSE_FLOAT_VECTOR | Milvus BM25 Function 输出 |

`content -> content_bm25 -> sparse_vector` 在 Schema 中一次建好。V1.0 只启用 Dense 查询，
后续再启用 BM25/Hybrid，不因此重建 Schema。第一版不加入 `metadata JSON`。

## 两类 MinIO 的职责

- `infra/compose.yaml` 中的 MinIO：RiskMind 原始 PDF、DOCX、Markdown 文档。
- `infra/compose.milvus.yaml` 中的 `milvus-minio`：Milvus 内部 segment/index/object storage。

两者不共享 bucket、凭据或业务职责。PostgreSQL 是 KnowledgeDocument 权威源，Milvus 只是
可重建的检索读模型。

## 执行探针

```powershell
python -m pip install -e "backend[knowledge,dev]"
docker compose -f infra/compose.milvus.yaml up -d
$env:SILICONFLOW_API_KEY="你的硅基流动 API Key"
python scripts/probe_retrieval_baseline.py
```

没有 API Key 时，可先验证 Runtime、Analyzer、BM25、Schema 和 Alias：

```powershell
python scripts/probe_retrieval_baseline.py --skip-dense
```

探针默认删除临时 Collection；传入 `--keep` 才保留。它会比较 `chinese`、`jieba`、`icu`
和 `standard` Analyzer，并打印每组 token 与 BM25 Top-3，避免只验证“Analyzer 能运行”。

## 实验状态

当前已完成不依赖 SiliconFlow Key 的实际探针：

1. Runtime：Milvus `2.6.24`、PyMilvus `2.6.17`，三个容器均通过健康检查；
2. Dense：SiliconFlow `BAAI/bge-m3` 已实际调用成功，返回向量通过 1024 维断言；
   7 个领域 Query 均为 Top-1 命中，使用 `COSINE`；
3. Analyzer：`jieba + removepunct` 会丢失 `0.42`、`CASE-2026`、`cnt_30`、`v3.2`；
   ICU 能保留 `credit_v3`、`apply_cnt_30d`、`v3.2` 和小数，最终候选冻结为
   `{"tokenizer": "icu", "filter": ["lowercase"]}`。`CASE-2026-018` 与 `T+30`
   会按标点切分，但查询侧使用相同 Analyzer，且 `case_id` 另有显式过滤字段；
4. BM25：4 组 Analyzer 在 7 个探针 query 上均为 Hit@3 `7/7`；`standard` 有两条不是
   Top-1，ICU 为 `7/7` Top-1；
5. Schema：Dense + BM25 Function 的完整候选 Schema 已实际创建、写入并检索；
6. Alias：已验证 `v001 -> active alias -> v002`，切换后经 alias 检索返回
   `document_version=v002`。

### ICU Analyzer 实际 token

下表省略 ICU 返回的空格 token，但保留标点 token：

| 输入 | token |
|---|---|
| `credit_v3 最近 KS 从 0.42 降到 0.31` | `credit_v3`, `最近`, `ks`, `从`, `0.42`, `降到`, `0.31` |
| `CASE-2026-018 的 apply_cnt_30d 出现 PSI 漂移` | `case`, `-`, `2026`, `-`, `018`, `的`, `apply_cnt_30d`, `出现`, `psi`, `漂移` |
| `模型 v3.2 在 T+30 标签成熟后性能恢复` | `模型`, `v3.2`, `在`, `t`, `+`, `30`, `标签`, `成熟`, `后`, `性能`, `恢复` |
| `credit_v3 和 credit_v2 的 KS 下降案例` | `credit_v3`, `和`, `credit_v2`, `的`, `ks`, `下降`, `案例` |

### ICU BM25 实际 Top-3

| Query | Top-3 chunk_id |
|---|---|
| `credit_v3 最近 KS 从 0.42 下降到 0.31` | `ks_label_maturity`, `feature_missing_rate`, `population_shift` |
| `apply_cnt_30d PSI 漂移历史案例` | `feature_psi_apply_cnt`, `score_distribution`, `model_known_limit` |
| `CASE-2026-018 调查过程` | `feature_psi_apply_cnt`, `feature_missing_rate`, `ks_label_maturity` |
| `T+30 标签成熟后性能恢复` | `ks_label_maturity`, `label_maturity_general`, `credit_v2_ks_case` |
| `credit_v3 v3.2 版本异常` | `model_v32_incident`, `population_shift`, `model_known_limit` |
| `客群结构变化导致 KS 下降` | `population_shift`, `feature_psi_apply_cnt`, `model_known_limit` |
| `credit_v2 KS 下降案例` | `credit_v2_ks_case`, `score_distribution`, `population_shift` |

附加验证：BM25 查询 `KS 下降案例` 配合过滤条件 `model_id == "credit_v2"`，只返回
`credit_v2_ks_case`。

### BAAI/bge-m3 Dense 实际 Top-3

| Query | Top-3 chunk_id |
|---|---|
| `credit_v3 最近 KS 从 0.42 下降到 0.31` | `ks_label_maturity`, `credit_v2_ks_case`, `population_shift` |
| `apply_cnt_30d PSI 漂移历史案例` | `feature_psi_apply_cnt`, `score_distribution`, `model_known_limit` |
| `CASE-2026-018 调查过程` | `feature_psi_apply_cnt`, `label_maturity_general`, `model_known_limit` |
| `T+30 标签成熟后性能恢复` | `ks_label_maturity`, `label_maturity_general`, `model_v32_incident` |
| `credit_v3 v3.2 版本异常` | `model_v32_incident`, `ks_label_maturity`, `model_known_limit` |
| `客群结构变化导致 KS 下降` | `population_shift`, `credit_v2_ks_case`, `score_distribution` |
| `credit_v2 KS 下降案例` | `credit_v2_ks_case`, `ks_label_maturity`, `population_shift` |

结论：这组小型领域探针中，1024 维 `BAAI/bge-m3` 没有出现基本语义区分失败。
该结果只用于冻结 Stage 0 技术基线，不替代 Stage 6 的正式检索评估。

最终 review 仍只关注以下六项：

1. Milvus / PyMilvus 实际版本；
2. `BAAI/bge-m3` 实际维度与领域 Dense Top-K；
3. Analyzer token 输出及最终选择；
4. BM25 Top-K；
5. Collection Schema；
6. Alias create / switch / search。
