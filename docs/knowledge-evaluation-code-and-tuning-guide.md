# RAG 评估系统：代码解析、算法拆解与指标驱动优化

本文解释当前 RiskMind 离线评测实现，并回答两个问题：评估能力还缺什么；指标变化后应优先调整哪一层。

公式使用 `$...$` 与 `$$...$$`。当前实测引用 2026-11-02 的 synthetic 数据集实验；本文中的调参方案是后续实验设计，不表示代码已经实现或策略已经有效。

## 1. 先分清四种评估对象

```text
问题 → 候选 Child → 排名后的 Child → 最终 Context → 生成答案
        召回能力      排序能力          上下文质量      生成质量
```

| 层次 | 主要问题 | 常用指标 |
|---|---|---|
| 候选召回 | 必要知识有没有进入候选集？ | Recall@K、Hit@K |
| 排序 | 相关内容是否排在前面？ | MRR、nDCG、Precision@K |
| 上下文组装 | 材料是否完整，是否过长？ | Quote Recall、Coverage、Context Precision、token |
| 答案生成 | 是否正确、忠实、有引用？ | Fact Recall、Correctness、Faithfulness、Citation |

不要看到一个 recall 下降就立刻更换 Embedding。必须先确认它测的是相关 Child、标准引用、最终 Context，还是答案事实。

## 2. 代码组成与边界

| 文件 | 角色 |
|---|---|
| `scripts/benchmark_knowledge.py` | 命令行入口、客户端配置、汇总、JSON 输出和退出码 |
| `knowledge/benchmark.py` | 数据集契约、语料核验、双策略成对实验、Context 指标 |
| `knowledge/retrieval_metrics.py` | 相关 Child 标签及召回/排名指标 |
| `knowledge/generation_evaluation.py` | 答案生成、结构化 LLM judge、确定性指标计算 |
| `scripts/_prepare_benchmark_dataset.py` | 生成固定 synthetic 语料和问题标注 |
| `knowledge_benchmark_v1.json` | 初始检索评测数据 |
| `knowledge_benchmark_v2.json` | 增加参考答案，供可选生成评估 |

评测调用已有 retrieval/context resolution，不在评测中修改切分算法、权威数据、集合或 serving route。环境准备、构建与激活必须在评测前完成。

## 3. 完整执行算法

```text
读取 JSON → BenchmarkDataset 校验
    ↓
确认 PG canonical 文档与数据集一致
    ↓
确认两种 profile 的 Child 与固定语料一致
    ↓
确认当前 serving 验收覆盖当前 PG 来源
    ↓
推导每个问题、每个 profile 的相关 Child 标签
    ↓
对每轮、每个问题，交替运行两种 profile
    ├── Dense Child Retrieval
    ├── Context Resolution
    ├── 路由稳定检查
    ├── Child 召回/排序指标
    ├── Context 覆盖/成本指标
    └── 可选：答案生成 → LLM judge → 生成指标
    ↓
结束时重新核验语料与路由
    ↓
CLI 汇总并写 JSON
```

固定数据集、相同问题和相同模型是可比性的基础。重复次数只能帮助观察运行波动，不能把六个问题重复三次说成十八个独立问题。

## 4. `benchmark.py`：数据契约

### 4.1 GoldQuote

保存 quote_id、document_id、document_version、text。它代表希望被覆盖的原文，不自动代表正式 Evidence。

### 4.2 BenchmarkQuestion

保存 question_id、KnowledgeSearchRequest、至少一条 relevant_quote，以及可选 reference_answer。

request 包括 query、filters、top_k；不是由评测执行器随意重写问题或自动放宽过滤。

### 4.3 BenchmarkDataset

保存 dataset_id、annotation_notes、文档和问题。validate_gold 检查文档版本与问题 ID 唯一、每题 quote ID 唯一、相同来源原文不重复，并要求 quote 完整出现在其源文档某一页。

跨页拼接的引用不会自动算合法；文档里不存在的“正确答案摘要”也不能冒充逐字原文引用。

### 4.4 配置语法

`extra='forbid'` 拒绝未知字段，`frozen=True` 禁止直接重新赋值。它们不是数据库约束，也不是递归深冻结。

`@model_validator(mode='after')` 在各字段通过检查后执行跨字段规则，`return self` 返回验证后的模型。

## 5. `verify_benchmark_corpus()`：公平比较之前的检查

1. PG 中相同 document_id/version 的完整 source_json 必须与数据集文档一致。
2. 在内存分别运行固定 Baseline 与 Parent-Child strategy，生成预期 Child。
3. 查询该 profile 的全部 PG Child，对照数量与 OUTPUT_FIELDS，拒绝额外语料或内容差异。
4. 解析该 profile 的 serving route。
5. 当前权威 Child 数量和摘要必须匹配当前集合的验收记录。

这些检查防止一套策略使用新语料、另一套使用旧语料。该函数不重新遍历 Milvus 全集合验收；实际候选在 retrieval 中仍会进行 PG 载荷检查。

## 6. 数据集摘要

dataset_digest 将模型转成 JSON 数据，移除 None 参考答案以兼容 v1，键排序并紧凑序列化后计算 SHA-256。

$$
D=SHA256(UTF8(CanonicalJSON(dataset)))
$$

列表顺序保留，因此文档/问题重排可能改变摘要。摘要用于标识内容版本，不是防止所有越权修改的签名。

## 7. `run_benchmark()`：实验循环

```python
for repeat in range(repeats):
    for index, question in enumerate(dataset.questions):
        profiles = list(IndexProfile)
        if (repeat + index) % 2:
            profiles.reverse()
        for profile in profiles:
            ...
```

`range(repeats)` 的轮次从 0 开始；`enumerate` 同时提供问题索引和对象；`% 2` 判断奇偶；reverse 让相邻实验交替策略顺序，减轻固定先后造成的预热偏差。

设问题数 Q、轮数 R、策略数 P：

$$
Runs=Q\times R\times P
$$

当前真实实验为 $6\times3\times2=36$。

没有并行两种策略来竞争服务资源，也没有完全随机化、独立 warmup 或统计显著性检验。

## 8. 时间统计与路由稳定

perf_counter 在检索前、检索后、解析后取时间：

$$
T_r=1000(t_1-t_0),\quad T_c=1000(t_2-t_1),\quad T_{total}=1000(t_2-t_0)
$$

单位为毫秒。检索包括 Embedding、Milvus、PG 校验；指标计算、tokenizer 与 judge 不包含在此 total 中。

每条结果 snapshot 必须匹配评测开始的 profile route；结束再次核验。逐次请求本身可以安全使用旧 snapshot，但整组对比不允许混合线上版本。

这是前后与逐次检查，不是持续锁定整个数据源。

## 9. `retrieval_metrics.py`：相关 Child 标签怎么来

relevant_child_ids 对全部固定语料分别构建 profile Child，若 Child 与某 gold quote 来源文档版本相同且完整包含该引用，就标为相关。

$$
G_{q,p}=\{c\mid Source(c)=Source(g),\ Text(g)\subseteq Text(c),\ g\in Gold(q)\}
$$

这里的包含是连续字符串匹配，不是语义向量相似性。

该集合来自完整语料，不只来自返回结果，避免把 Recall 分母错误地设成“已经找回的相关项”。

但它不是穷尽语义标注：相关片段没有被标注、引用跨 Child 边界，都会影响解释。不同策略的 G 可能不同，所以跨策略比较必须同时看 quote 和最终 Context 指标。

## 10. 召回与排序指标公式

设请求 k、返回 n≤k、相关集合 G，第 i 条相关性 $r_i\in\{0,1\}$，命中数 $m=\sum r_i$。

### 10.1 Precision@K

$$
P@k=\frac{m}{k}
$$

当前分母是请求 k，不是实际返回 n；不足 k 的位置按未命中处理。

### 10.2 Recall@K

$$
R@k=\frac{m}{|G|}
$$

相关 Child 共 4 个，返回命中 3 个，Recall=75%。G 为空时为 null，不代表 0 或 100%。

如果增加 k 且候选严格保持同一排序前缀，Recall 不会下降；实际 ANN 搜索参数也会随 k 变化，不能把这一理想性质当成所有实际请求的保证。Precision 则可能升也可能降。

### 10.3 Hit@K 与 RR/MRR

$$
Hit@k=\mathbf{1}[m>0]
$$

$$
RR@k=\begin{cases}1/j,&j=\min\{i:r_i=1\}\\0,&m=0\end{cases}
$$

首个相关结果在第 1 位则 RR=1，在第 5 位则 RR=0.2。行级字段虽然叫 retrieval_mrr_at_k，单条实际是 RR；跨问题均值才是 MRR。

### 10.4 nDCG

$$
DCG@k=\sum_{i=1}^{n}\frac{r_i}{\log_2(i+1)}
$$

$$
IDCG@k=\sum_{i=1}^{\min(k,|G|)}\frac{1}{\log_2(i+1)},\quad nDCG@k=\frac{DCG@k}{IDCG@k}
$$

相关项越靠前越好；当前是二元相关性，没有“关键根因=3、辅助背景=1”之类分级权重。G 为空时 nDCG 也为 null。

### 10.5 输入完整性

重复 Child ID、超过 k、rank 不按 1…N 排列，都直接失败。指标模块不偷偷去重或重新排序后算分。

## 11. `measure_contexts()`：为什么还要一组 Context 指标

Child Recall 回答搜索找到了什么，Context Recall 回答最终交给模型的材料完整到什么程度。Parent 展开可能让两者发生差异。

先验证原 snapshot 一致；再验证所有 supporting_hits 恰好分配原始命中 Child，不丢失、不重复、不改载荷。

内部 matches(text,hits) 只有在支持 Child 的文档版本与 quote 一致，且正文完整包含该 quote 时才记录其 quote_id。

定义 gold 引用集合 Q；Child 正文覆盖的引用集合为 $Q_C$；最终 Context 覆盖的为 $Q_X$。

$$
ChildQuoteRecall=\frac{|Q_C|}{|Q|},\qquad ContextQuoteRecall=\frac{|Q_X|}{|Q|}
$$

两者分母都是固定标准引用数，与相关 Child 集合 G 不同。

## 12. Context precision、coverage 与完整性

设返回 Context 序列为 X，每份覆盖的引用集合为 $Q_x$：

$$
ContextPrecision=\frac{\sum_{x\in X}\mathbf{1}[|Q_x|>0]}{|X|}
$$

空 Context 时当前实现为 0。它是 Context 条数比例，不是逐 token 精确率；一个 1600 字 Parent 只包含一句标准引用也会算作相关 Context。

$$
Coverage=\frac{\sum_{g\in Q_X}len(text_g)}{\sum_{g\in Q}len(text_g)}
$$

只给完整包含的引用按字符长度加权，不给部分匹配得分。

$$
CompleteContext=\mathbf{1}[Q_X=Q]
$$

长引用权重更高是当前代理口径，不代表业务重要性更高。

## 13. 成本与冗余

$$
Expansion=\frac{\sum_{x\in X}len(content_x)}{\sum_{c\in Retrieved}len(content_c)}
$$

Child 字符总数为 0 时为 null。这个比值不等于 token 比例；平均逐请求比值也不等于汇总字符数之比。

$$
RedundantGold=\frac{\sum_x|Q_x|-|\bigcup_xQ_x|}{\sum_x|Q_x|}
$$

分母为 0 时当前记 0。它只衡量 gold quote 跨 Context 重复，不是任意正文的重复率。

context_tokens 由传入的固定 tokenizer 对每份 Context 正文独立计数后相加，不包含标题、问题、提示词和答案。未提供 tokenizer 时为 null。

## 14. `generation_evaluation.py`：两次模型请求

```text
回答模型：问题 + 实际 Context → 答案
评审模型：问题 + Context + 答案 + 参考答案 + 标准事实 → JSON 判断
Python：校验 JSON 与对应关系 → 计算指标
```

参考答案和 gold facts 不提供给回答模型，避免答案泄漏。Context 注入一次正文，不重复附上所有 supporting Child 正文。

回答模型与 judge 均需明确提供模型 ID，temperature=0。该参数不保证外部服务完全确定性。

同模型评审会被报告 same_model_judge，便于识别偏差风险，但代码不会自动阻止使用同模型。

## 15. 结构化评审数据契约

| 模型 | 记录内容 |
|---|---|
| ClaimAssessment | 原子陈述、是否正确、是否被上下文支持、支持引用编号 |
| FactAssessment | 每条 gold fact 是否正确出现在答案中 |
| CitationAssessment | 每个实际引用编号是否支持答案 |
| GenerationAssessment | 上述项目、问题相关性与评审理由 |

使用 StrictBool/StrictInt，避免把字符串或布尔值错误地当作对应类型。无法判断用 None；有未知项就不能发布确定分数。

通过 response_format=json_object 请求 JSON，并在提示中提供 schema，最终仍由 Pydantic 校验；这不是依赖服务端严格 JSON Schema 模式保证一切。

## 16. `measure_generation()` 的校验步骤

1. 重新执行评审模型契约校验。
2. fact_id 必须完整覆盖 gold facts，不重复、不新增。
3. claim 文本不能为空或完全重复；评审理由不能为空。
4. 正则读取答案实际引用的数字编号，judge 必须对每个不同编号给出判断。
5. 支持引用只能指向实际存在的 Context。
6. 每个 claim 的支持编号必须唯一，且属于答案实际使用、存在、被判支持的编号。
7. 有支持引用的 claim 必须同时判为上下文支持。
8. 任意 None 判断使结果不确定。
9. 没有正确 claim 却声称覆盖参考事实，判为矛盾。

重要限制：schema 和代码能检查结构与 ID 覆盖，不能独立证明 judge 真把答案所有原子陈述都列出来。LLM 漏拆陈述或误判仍需人工校准。

## 17. 生成指标公式

设原子陈述 N 条，正确 $N_c$ 条，上下文支持 $N_s$ 条；标准事实 F 条，正确覆盖 $F_c$ 条。

$$
FactPrecision=\frac{N_c}{N},\qquad FactRecall=\frac{F_c}{F}
$$

当前 answer_correctness 是这两者的自定义 F1：

$$
Correctness=\frac{2PR}{P+R}
$$

不是直接复用第三方框架某个同名指标。N=0 时 precision 为 0，faithfulness/hallucination 为 null。

$$
Faithfulness=\frac{N_s}{N},\qquad HallucinationRate=1-Faithfulness
$$

这里幻觉按“无上下文支持或矛盾”定义，不等于现实世界一定虚假。Context 本身可以包含错误，因而忠实度高也不必然正确。

answer_relevancy 是 judge 按固定 rubric 输出的 0～1 相关性评分，不由上述计数推导。

## 18. 引用指标口径

令 U 为答案实际出现的不同数字引用编号集合，V 为有效 Context 编号集合，S 为 judge 判支持的编号。

$$
CitationPrecision=\frac{|S|}{|U|}
$$

$$
CitationRecall=\frac{\#\{claim\mid supporting\_citations\neq\varnothing\}}{N}
$$

$$
InvalidCitationRate=\frac{|U\setminus V|}{|U|}
$$

无引用或无 claim 时对应分母不定义，代码返回 null。重复出现同一编号按不同编号集合计，不按每次出现次数计。

`[0]` 也会被识别并计为无效编号。当前正则只识别 `[数字]`，不能当成任意 Markdown 引用格式解析器。

## 19. 生成评审的报告与失败处理

记录模型 ID、rubric 版本、提示词 SHA、Context 摘要、snapshot、答案、结构化判断、耗时和 token usage。

回答或 judge finish_reason 不是 stop、JSON 不合法、缺少判断、事实 ID 错误等，都保持 INCONCLUSIVE，不输出确定 metrics；错误报告只保存异常类型，避免泄露凭据。

usage 在响应后尽早保存，失败仍可能已消耗 token。模型没有返回 usage 时为 null，不编造为 0。

答案链路耗时为 retrieval+resolution+generation，不含 judge；judge 是离线评估成本，不是用户等待答案所必需的阶段。

## 20. CLI 汇总逻辑

每个 profile 分组，数值/布尔指标取宏平均。含 None 的数值指标不静默只对已定义子集取平均，而保持未知。

生成评审有失败时，不拿成功子集均值冒充完整实验结果。报告区分 retrieval_status 与 generation_status。

端到端 p50 使用 median，p95 使用 nearest-rank：

$$
P95=x_{\lceil0.95n\rceil}
$$

其中序列按耗时升序，公式使用 1-based 下标。18 个样本时 p95 恰好是最大值，很容易被一个冷启动影响。

全流程执行成功但分数低，仍可能 status=SUCCEEDED；状态代表执行完成，不是质量达标。检索完整性异常向上传播，CLI 输出 INCONCLUSIVE 并返回退出码 2。

## 21. 本次真实实验能够说明什么

真实 BGE-M3 + PG + Milvus、六个问题、三轮、36 条记录：

| 指标 | Baseline | Parent-Child |
|---|---:|---:|
| Child Quote Recall | 43.06% | 43.06% |
| Context Quote Recall | 43.06% | 54.17% |
| Context Precision | 16.67% | 22.22% |
| 全部引用完整覆盖比例 | 16.67% | 33.33% |
| 平均 Context 字符数 | 863.17 | 1259.00 |
| 延迟中位数 | 151.16 ms | 171.05 ms |

主要收益出现在行动建议问题：Parent 展开让引用召回从 33.33% 到 100%。根因问题仍为 25%，成熟度问题仍为 0%。

结论是“当前样本出现局部展开收益”，不是“Parent-Child 全面优于 Baseline”。本轮生成侧为 NOT_RUN，tokens 未测。

## 22. 当前评估还缺什么

### 22.1 数据与标注

- 真实长报告、跨章节问题、数字/表格/缩写/版本问题，以及无答案和易混淆问题。
- 独立领域 review、足够多的独立问题与分层统计。
- 语义相关性标注、关键事实重要性，避免只依赖逐字引用。
- 调参集与独立测试集，防止围绕六个固定问题过拟合。
- 明确无答案/拒答正确性和过滤权限场景，不能只测试可回答问题。

### 22.2 生成与成本

- 显式选定答案模型和 judge 模型后跑真实生成评测。
- 由人工抽查 judge 的 claim 划分与判断，记录分歧；必要时多 judge 复核。
- 固定下游 tokenizer，测 Context token、完整 prompt token、生成 token 和费用。
- 记录模型版本/服务变更、依赖版本、检索参数与索引配置等更完整实验清单。

### 22.3 诊断与统计工具

- 逐条 missing quote 的“源文档→Child→候选 rank→Context→答案”追踪表。
- Recall@5/10/20 等曲线，以及候选 K 与最终输出 k 的独立口径。
- 固定 token budget 的公平比较，而不只固定 top_k。
- 独立 warmup、冷/热分开、并发吞吐与故障率。
- 以问题为单位的配对差异和置信区间；重复请求不当作独立问题。
- 明确版本化通过门槛与回归报告，当前没有自动质量达标判定。

这些是评测完善项，不意味着现在要立刻修改 sealed 检索或引入全部优化组件。

## 23. 指标变化先问五个问题

1. 指标具体是哪一种 recall？分母是什么？
2. 数据集、gold、过滤条件、模型、route 是否与上一轮相同？
3. 相关内容在源文档和索引里真的存在吗？
4. 它没进候选，还是进了但排序/展开/生成阶段丢失？
5. 改善一个指标是否以过高的 token、延迟或忠实度下降为代价？

没有完成归因前，不根据一项宏平均直接决定换模型。

## 24. 指标→故障层→候选策略决策表

| 现象 | 优先怀疑 | 先做的诊断 | 后续候选策略 |
|---|---|---|---|
| Child Recall@5 低，但 Recall@20 明显高 | 排名/截断 | 看相关 Child 的实际位置 | 扩大候选 K，再用 Reranker 选最终 k |
| Recall@20 仍低 | 索引、语料、Embedding、词面匹配或过滤 | 查 gold 是否有可达 Child；比较 Dense/BM25 候选 | 修复来源/过滤；Dense+BM25；再比较模型或输入 |
| Recall 高，MRR/nDCG 低 | 相关项靠后 | 看第一条及关键项 rank | Reranker、融合排序、难负样本分析 |
| Recall 高，Precision 低 | 候选噪声或标注不全 | 人工看“未标注但相关”与真正噪声 | 保留宽候选，重排后收紧输出与预算 |
| Child Recall 低，但 Context Recall 高 | Parent 展开补回材料 | 看是否只依赖一个局部命中 | 保留有收益展开，同时改进候选召回 |
| Child Recall 高，Context Recall 低 | gold 跨块、映射/组装或指标口径 | 比较 quote 内容、链接、最终正文 | 调整未来构建 profile 或上下文选择；先排除标注问题 |
| Context Recall 高，token 大增，正确性不升 | Parent 过宽或上下文噪声 | 检查预算、关键段落位置和答案遗漏 | 更精确 Context 选择、段落裁剪或压缩实验 |
| Context 足够，答案 Fact Recall 低 | 生成遗漏或过度拒答 | 对照缺事实是否在输入内 | 提示结构、问题分解、生成模型、输出预算 |
| 忠实度低/幻觉高 | 无支持扩写、证据混淆 | 逐 claim 核验 | 强化仅据 Context 回答、缺资料时拒答、改生成模型 |
| 忠实度高、正确性低 | Context 本身错误或参考标注冲突 | 审查文档版本和事实 | 修正语料/标注，不只改生成提示 |
| 引用精确率低 | 编号/归因错误 | claim→引用→Context 对照 | 引用格式与归因校验、修正生成提示 |
| 分数好但延迟大 | 上游、线程队列、PG、Context 长度 | 分段计时和冷/热对照 | 针对瓶颈做缓存/并发/请求预算实验 |

表中策略均为假设，需要同条件 A/B 验证。不能把相关内容没入候选的问题交给纯重排器解决。

## 25. 最重要的区分：召回 K 与最终 k

当前接口只有 request.top_k，一次搜索结果直接进入 Context Resolution。

未来引入两阶段检索时应明确：

```text
候选召回 K=20
    ↓
重排
    ↓
最终保留 k=5
    ↓
Context Resolution / token budget
```

K 与 k 只是示例，不是已经测定的最佳参数，也不是当前接口已支持的两个参数。

纯重排满足：

$$
Output_{rerank}\subseteq Candidates
$$

所以候选池完全没有的材料，重排不能凭空补出。先测候选召回，再测最终排序。

Cross-Encoder 联合输入 query 与候选打分，是典型二阶段重排方法；其角色与快速候选召回不同。参见 [Sentence Transformers 官方 Retrieve & Re-Rank](https://www.sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html)。

## 26. 什么时候考虑 BM25 / Hybrid

如果失败集中在模型名、错误码、业务缩写、表格列名、精确数字或关键词，先比较词面召回能否补回漏项，而不是直接认定语义模型失效。

候选组合可表达为：

$$
C=C_{dense}\cup C_{lexical}
$$

这能扩大候选来源，但也可能增加噪声与延迟，需要再评估排序与输出预算。

Dense 和 BM25 原始分数不在同一尺度，不能未经校准直接相加。基于排名的融合是可实验方式之一；融合与模型重排不是完全相同的组件。

Milvus 官方介绍了 Dense/Sparse 多向量搜索与结果融合能力；这说明可实现路径，不是当前项目已接入 Hybrid 的证明。参见 [Milvus Multi-Vector Hybrid Search](https://milvus.io/docs/multi-vector-search.md)。

## 27. 什么时候改切分或 Parent

先判断可达性：gold 在原文中存在，但没有任何 Child 能完整覆盖，可能是 quote 跨边界或标注粒度问题；不能只靠换 Reranker 修复。

Child 太短可能缺语义背景，太长可能主题混杂。Parent 过大增加噪声和 token，过小不能覆盖完整机制。最佳大小必须由报告类型和问题测试决定。

修改 400/60、Parent 参数、标题拼接或 ID 规则会影响索引身份与向量，必须采用新的 scheme/index profile 或其他明确版本化方案，重新构建、验收和激活。不要原地污染已经封板的 baseline-v1/parent-child-v1。

## 28. 怎样判断提高 Recall 是否值得

一个示例，不是本项目实测：

| 方案 | Context Recall | 答案正确性 | 平均 Context token | p95 |
|---|---:|---:|---:|---:|
| A | 0.60 | 0.70 | 1200 | 500 ms |
| B | 0.80 | 0.71 | 6000 | 1500 ms |
| C | 0.76 | 0.80 | 1800 | 700 ms |

B 召回最高，但成本增加很多且答案收益小；若业务约束允许，C 可能更值得选。选择依据是预先约定的质量、成本和延迟约束，而非单指标最大化。

可以用 Pareto 比较：若某方案质量不低、成本与延迟都不高，并至少一项更好，则它支配另一个方案。没有支配关系时，需要业务权衡。

## 29. 结合本次缺口的具体实验顺序

1. 对成熟度、根因、标签定义、特征数量的 missing quote，定位源文档与完整相关 Child。
2. 保持现有两套构建不动，用版本化数据集副本测 top_k=5/10/20；修改 request 后应更新实验标识与数据摘要。
3. 记录相关 Child 首次出现的 rank、各 k 下引用覆盖、Context 字符/token 与延迟。
4. 如果相关项在更大 K 中出现，优先验证未来的“宽召回+重排+窄输出”。
5. 如果大 K 仍找不到，核对过滤、可达性和 ANN，再测试词面召回及 Embedding 输入等独立因素。
6. 指定答案/judge 模型后，验证 Context 改善是否传递成答案事实召回与正确性改善。

当前尚未做上述 K 扫描，因此不能断言本次成熟度问题一定是排名问题或一定需要 BM25。

## 30. 可执行的实验纪律

- 每次只改一个因素，保留固定对照组。
- 数据、gold、模型、参数、route、代码版本要可追溯。
- 使用独立评测环境，不让正在变化的生产路由污染实验。
- 按问题类别查看差异，不只看总平均。
- 缺失指标保留 null，依赖失败保留 INCONCLUSIVE。
- 不因某轮分数不好而删样本；冷启动与热运行按预先声明口径分开。
- 先用调参集选方案，再用未参与选择的测试集确认。
- 不凭一次提升宣布泛化收益；配对统计以独立问题为单位。

## 31. 常见 Python 语法与代码读法

| 写法 | 用途 |
|---|---|
| frozenset(...) | 保存不可变相关 ID 集合 |
| 字典键 `(question_id, profile)` | 区分每题、每策略标签 |
| set().union(*sets) | 将多个引用集合取并集，空输入也得到空集合 |
| sum(bool(ids) for ids in context_sets) | 统计至少包含一条引用的 Context 数 |
| metrics.update(other) | 合并召回指标与 Context 指标 |
| **pipeline_options | 将可选会话/客户端参数转交检索函数 |
| perf_counter() | 测耗时，不作日历时间 |
| setdefault(key, value) | 读取已有基准，缺失时登记基准 |
| Literal / StrictBool / StrictInt | 固定允许值或严格类型规则 |
| re.findall(r'\[(\d+)\]', answer) | 提取数字引用编号 |
| try/except/finally | 输出失败状态并释放评审客户端 |

## 32. 推荐阅读顺序

1. benchmark.py 的数据模型：理解标注与实验对象。
2. retrieval_metrics.py：先掌握 Precision/Recall/MRR/nDCG 分母。
3. measure_contexts：理解 Child 与 Parent 展开的差异如何被测量。
4. run_benchmark：理解成对执行、计时与快照限制。
5. generation_evaluation.py：理解 LLM 判断与确定性计算的边界。
6. scripts/benchmark_knowledge.py：查看命令行、汇总和报告。

## 33. 源码、结果与运行说明

- [评测主流程](../backend/src/risk_platform/knowledge/benchmark.py)
- [召回指标](../backend/src/risk_platform/knowledge/retrieval_metrics.py)
- [生成评审](../backend/src/risk_platform/knowledge/generation_evaluation.py)
- [运行入口](../scripts/benchmark_knowledge.py)
- [数据集准备](../scripts/_prepare_benchmark_dataset.py)
- [v2 数据集](../backend/tests/fixtures/knowledge_benchmark_v2.json)
- [评测主流程测试](../backend/tests/knowledge/test_benchmark.py)
- [召回指标测试](../backend/tests/knowledge/test_retrieval_metrics.py)
- [生成评审测试](../backend/tests/knowledge/test_generation_evaluation.py)
- [评测运行说明](runbooks/knowledge-evaluation.md)
- [真实实验分析](knowledge-benchmark-real-20261102.md)
- [真实实验原始报告](knowledge-benchmark-real-20261102.json)
- [Embedding 与 Retrieval 解析](knowledge-embedding-retrieval-guide.md)
- [Context Resolution 解析](knowledge-context-resolution-guide.md)

## 34. 最终诊断口诀

```text
没进候选：查数据、过滤、切分、向量、词面召回。
进了但靠后：查排序，考虑宽召回后的重排。
命中了但材料不全：查 Parent/Context 组装与覆盖。
材料全但答错：查生成、提示、引用与评审标注。
质量变好但代价太大：按固定 token/延迟预算重新比较。
任何调整之前：先确认指标口径和实验条件没变。
```
