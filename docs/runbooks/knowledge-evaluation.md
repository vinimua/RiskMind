# RAG 召回侧与生成侧评测

本评测扩展既有 Stage 6E benchmark，不修改分块、PG/Milvus schema、serving、检索或 Context 解析。
生成答案与 LLM judge 仅用于离线评测，不得充当正式 Evidence、根因判定或生产操作指令。

## 1. 执行链路

```text
固定问题 / 语料 / gold quotes / reference answer
        ↓
两种 profile 各自的 serving route snapshot
        ↓
Dense Child Retrieval → 召回排序指标
        ↓
Context Resolution → Context 覆盖和成本指标
        ↓ --evaluate-generation
同一回答模型分别依据两种 profile 的 Context 生成答案
        ↓
显式指定的 LLM judge 给出结构化细项判断
        ↓
代码校验事实 ID、引用编号及判断完整性，再计算生成侧指标
```

参考答案及 gold facts **只提供给 judge，不提供给回答模型**。
模型输入中的 Context 仅注入一次正文，并携带标题、章节、模型标识及 Child 来源 ID；
不会重复附上 supporting Child 正文。原始 supporting_hits 仍保留在 benchmark 结果供审计。
两个 profile 共用相同问题、生成模型、评审模型、提示词和 temperature=0。
temperature=0 不保证模型服务完全确定性，仍应重复运行并校准人工样本。

## 2. 召回侧指标

实现：`backend/src/risk_platform/knowledge/retrieval_metrics.py`。

### 2.1 Gold Child 集合如何定义

对完整固定语料分别运行已封板的 Baseline / Parent-Child 纯构建策略。
若 Child 的 document_id、document_version 与 gold quote 一致，且完整包含至少一条 gold quote，
则该 Child 属于该 profile、该问题的相关集合。报告记录：

```text
retrieval_label_basis = same-source-verbatim-gold-quote-v1
```

相关集合是完整语料中的集合，不是只根据返回结果构造，避免错误的 Recall 分母。
这是确定性的 quote 标注口径，不是穷尽的人类语义相关性；原文引用跨 Child 边界时，
可能没有任何单个 Child 完整包含它。两个 profile 的相关 Child 数可能不同，
因此仍需结合原有 quote recall 和最终 Context recall 比较。

### 2.2 公式

设请求数量为 $k$，完整相关 Child 集合为 $G$，第 $i$ 个返回结果是否相关为 $r_i\in\{0,1\}$，
实际返回数量为 $n\leq k$，命中数 $m=\sum_{i=1}^{n}r_i$。

$$
\mathrm{Precision@k}=\frac{m}{k}
$$

分母固定为请求 k，返回不足 k 的位置视为未命中。

$$
\mathrm{Recall@k}=\frac{m}{|G|}
$$

$$
\mathrm{Hit@k}=\mathbf{1}[m>0]
$$

单问题 reciprocal rank：

$$
\mathrm{RR@k}=\begin{cases}1/j,&j=\min\{i:r_i=1\}\\0,&m=0\end{cases}
$$

行级字段为 `retrieval_mrr_at_k`，跨问题取均值才是 MRR。

二元相关性的折损累计增益：

$$
\mathrm{DCG@k}=\sum_{i=1}^{n}\frac{r_i}{\log_2(i+1)}
$$

$$
\mathrm{IDCG@k}=\sum_{i=1}^{\min(k,|G|)}\frac{1}{\log_2(i+1)}
$$

$$
\mathrm{nDCG@k}=\frac{\mathrm{DCG@k}}{\mathrm{IDCG@k}}
$$

若没有可达相关 Child，即 $|G|=0$，Recall 和 nDCG 为 null，不能记为完美或用零掩盖分母缺失。
已知存在相关 Child 而返回为空时，Recall、RR、nDCG 均为零。
重复 ID、超出 k、排名不连续直接失败。

已有 `child_relevant_quote_recall`、`context_relevant_quote_recall`、Context precision/coverage、
扩展比例、冗余、token 和延迟指标继续保留，定义见 [原 benchmark runbook](knowledge-benchmark.md)。

## 3. 生成侧指标

实现：`backend/src/risk_platform/knowledge/generation_evaluation.py`。

### 3.1 评审输入和输出

输入包含问题、生成答案、实际 Context、来源 Child、参考答案以及每条 gold quote 的稳定 fact_id。
回答使用 `[1]`、`[2]` 等引用当前请求 Context 的编号，编号与完整 context_id 的对应关系保留在原始结果中。

Judge 必须返回：

- `claims`：答案中全部可核验原子事实陈述，以及是否被 Context 支持、是否正确、有哪些支持引用。
- `reference_facts`：每条 gold fact 是否在答案中被正确覆盖，ID 必须完整且不重复。
- `citations`：答案实际使用的每个不同引用编号是否支持答案。
- `answer_relevancy`：按固定 rubric 给出 0..1 的问题相关性。
- `rationale`：评审理由，供人工复核。

评审结果通过 Pydantic 结构校验；不是直接让模型随意返回一串总分。
不确定的事实判断用 null；出现此类判断时整条生成评测为 INCONCLUSIVE，不发布确定指标。

### 3.2 正确性与完整性

设生成答案被拆成 $N$ 个事实陈述，其中 judge 判定正确的数量为 $N_c$；
参考事实总数为 $F$，被正确覆盖的数量为 $F_c$。

$$
P_a=\frac{N_c}{N},\qquad R_a=\frac{F_c}{F}
$$

$$
\mathrm{AnswerCorrectness}=\frac{2P_aR_a}{P_a+R_a}
$$

对应 `answer_fact_precision`、`answer_fact_recall` 和 `answer_correctness`。
无事实陈述时事实精度取零；精度与召回均为零时正确性取零。
该分数是本项目 `rag-generation-v1` rubric 的事实 F1，**不是 Ragas 官方实现或某种通用标准分数**。
句子拆解粒度会影响该分数，必须保留明细并做人工校准。

### 3.3 忠实度和幻觉率

设 $N_s$ 个陈述得到检索 Context 支持：

$$
\mathrm{Faithfulness}=\frac{N_s}{N}
$$

$$
\mathrm{HallucinationRate}=\frac{N-N_s}{N}
$$

这里“幻觉”指无当前 Context 支持，不等于模型陈述在现实世界中一定错误。
答案可能忠于不充分的 Context，却漏掉 gold facts，因此需同时看正确性与完整性。
无事实陈述时两者为 null，不把空答案或拒答记为 100% 忠实。

### 3.4 问题相关性

`answer_relevancy` 由 LLM 按固定量表评审：

- 0：完全无关。
- 0.5：部分回应，但有明显偏题。
- 1：直接回应问题。

它是模型评审意见，不是向量相似度，也不是确定性真值。

### 3.5 引用指标

设答案使用了 $U$ 个不同编号，judge 判定支持答案的有效编号数为 $U_s$，
有至少一个支持引用的事实陈述数量为 $N_{cite}$，不存在的编号数量为 $U_{bad}$。

$$
\mathrm{CitationPrecision}=\frac{U_s}{U}
$$

$$
\mathrm{CitationRecall}=\frac{N_{cite}}{N}
$$

$$
\mathrm{InvalidCitationRate}=\frac{U_{bad}}{U}
$$

同一编号重复使用只计一次引用 precision 分母；引用 recall 按事实陈述计算。
无引用时引用 precision / invalid rate 为 null，有事实但无支持引用时引用 recall 为零。
不存在的编号不能被评为有效支持，claim 也不能凭空引用答案未用过的编号。
当前约定数字方括号为引用，答案中的 `[1]` 一类表达都会被按引用处理。

### 3.6 成本与延迟

- `generation_ms`：回答 API 完整调用耗时。
- `judge_ms`：评审 API 完整调用耗时，与回答耗时分开。
- `answer_pipeline_ms`：检索、Context 解析加答案生成的耗时，不包含离线 judge。
- `prompt_tokens` / `completion_tokens`：回答模型返回的 usage。
- `judge_prompt_tokens` / `judge_completion_tokens`：评审模型 usage。
- usage 不可用时为 null，不能用字符数替代。
- 原 `retrieval_ms`、`resolution_ms`、`total_ms` 仍只包含检索和 Context 解析，不因开启生成而改变定义。

## 4. 数据集版本

保留 `knowledge_benchmark_v1.json` 原有内容和 digest。
新增 `backend/tests/fixtures/knowledge_benchmark_v2.json`，同样两份 synthetic 文档、六个问题，补充六份参考答案。

```powershell
python scripts/_prepare_benchmark_dataset.py --with-reference-answers
```

默认不加此参数仍生成 v1。参考答案和 quotes 都是初始人工编写标注，需要领域验收，不能外推生产准确率。
已有 v1 可继续跑召回侧；开启生成侧时缺少 reference_answer 会在模型调用前失败。

## 5. 运行方式

先按原 runbook 准备专用 PG/Milvus 环境及两个已经验收、激活的 serving routes。
保持同一份 canonical 文档与语料快照，不要在实验中 staging 或切换 route。

仅召回侧：

```powershell
python scripts/benchmark_knowledge.py `
  --dataset backend/tests/fixtures/knowledge_benchmark_v1.json `
  --output retrieval-evaluation.json --repeats 3
```

召回加生成侧：

```powershell
python scripts/benchmark_knowledge.py `
  --dataset backend/tests/fixtures/knowledge_benchmark_v2.json `
  --output full-rag-evaluation.json --repeats 3 `
  --evaluate-generation `
  --answer-model "实际生成模型ID" `
  --judge-model "实际评审模型ID"
```

不默认选择、下载或虚构一个可用的模型。两个模型须支持所用 OpenAI-compatible Chat Completions 接口；
judge 还需接受 `response_format={"type":"json_object"}`。不支持的服务会显式失败，不静默改用另一模型。

配置：

| 环境变量 | 用途 |
|---|---|
| `RISK_DATABASE_URL`、`MILVUS_URI` | 专用评测存储 |
| `SILICONFLOW_API_KEY`、可选 `SILICONFLOW_BASE_URL` | 既有 Embedding 配置 |
| `RAG_EVAL_API_KEY` | 可选生成/评审 Key，未设时复用 SiliconFlow Key |
| `RAG_EVAL_BASE_URL` | 可选生成/评审 OpenAI-compatible endpoint，未设时使用 SiliconFlow endpoint |

当前回答与评审模型共用同一生成评审 endpoint；通过两个模型参数指定各自 ID。
优先选择不同模型并做人工抽查。允许同模型但报告会记录 `same_model_judge=true`，不能忽视自评偏差。
客户端 timeout 为 60 秒、自动重试为零，避免失败后静默重复消耗；没有工具调用或生产副作用。

## 6. 结果和失败语义

每条记录保留原始检索命中、Context、snapshot、生成答案、模型名、提示词 SHA256、
rubric_version、Context 输入摘要、评审细项、成本与耗时。

```text
默认只评召回：generation_status = NOT_RUN
召回和全部生成评审成功：status = SUCCEEDED
召回成功，但任意生成/评审失败：status = INCONCLUSIVE
```

生成侧失败保留召回结果及错误类型，不附上伪造生成分数，也不对成功子集发布生成宏平均。
报告区分 retrieval_status 与 generation_status，命令退出码 2 表示整次实验不完整。
若检索完整性校验失败，则沿用原逻辑，不发布部分检索结果。

凭据和上游异常正文不写入报告。答案本身与原文会保留，实际业务数据需按其访问权限管理。

## 7. 解释边界

1. 召回指标依据不穷尽的完整引用标注，不等同于人工穷尽语义相关性。
2. LLM judge 可能漏拆事实、误判、受措辞或数据中的指令影响；结构校验不能证明语义判断真实。
3. Prompt 明确把知识和问题视为数据，但这不构成对 prompt injection 的形式化防御。
4. 同模型自评存在偏差，temperature=0 也不能消除服务端随机性与模型更新影响。
5. 当前没有自动的价格换算；token usage 只是成本输入，不虚构金额。
6. 本实现和模拟测试不代表已运行真实模型实验；模型选择与真实依赖配置完成后才可发布 A/B 数值结论。
