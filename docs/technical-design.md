# 信贷风控模型异常调查与闭环处置系统
## 技术开发框架文档 V1.3

**适用项目：** 信贷风控模型智能监测与自主迭代平台  
**文档定位：** 系统总体框架基线  
**版本：** V1.3
**日期：** 2026-10-13

> **核心原则：先规划、后执行；先观察、后重规划；先证据、后根因；先门禁、后处置。**
>
> 系统采用 **Plan-and-Execute + ReAct-style Replanning** 的调查模式。  
> RAG、事实检索和证据计算只是任务执行能力，不承担工作流编排职责；  
> Policy Gate 负责确定性根因判定；处置阶段采用 **Plan → Gate → Execute → Validate**，不允许自由 ReAct。

# 目录

1. 系统目标
2. 总体设计思想
3. 总体架构
4. Investigation Agent
5. Initial Planning
6. Investigation Task 与 Capability
7. Task Execution
8. Observation 与 Investigation State
9. Replanning
10. Evidence Qualification 与 Evidence Store
11. Policy Gate 与根因判定
12. 处置与验证闭环
13. 知识库设计
14. LangGraph 状态与核心数据对象
15. V1 开发边界与扩展接口

# 1. 系统目标

本系统用于处理信贷风控模型运行过程中的性能异常、数据异常、特征漂移、客群变化、版本异常等问题，形成从异常触发、调查规划、任务执行、观察结果、动态重规划、证据确认、根因判定，到处置、验证、发布、观察和知识沉淀的闭环。

V1 重点建立稳定的系统骨架，而不是一次性实现复杂多 Agent 架构。

系统首先解决：

- 如何把“为什么异常”拆成可执行调查任务；
- 如何调用知识、事实和计算工具完成任务；
- 如何根据工具返回结果动态调整后续调查；
- 如何区分“观察结果”和“可正式用于根因判定的证据”；
- 如何在根因确认后进入受控处置流程；
- 如何为后续子 Agent、专业调查模块和知识图谱预留接口。

# 2. 总体设计思想

调查阶段采用：

```text
Initial Planning
      ↓
Task Execution
      ↓
Observation
      ↓
Investigation State Update
      ↓
Replanning
      ↓
继续调查 / 调查充分
```

其中 `Observation → Replanning` 体现 ReAct 思想：

```text
Reason
  ↓
Act
  ↓
Observe
  ↓
Reason Again
```

但系统不是纯 ReAct，而是：

> **先形成初始计划，再根据执行结果进行有限、受控的 Replanning。**

因此采用：

> **Plan-and-Execute + ReAct-style Replanning**

处置阶段不允许自由 ReAct，而采用：

```text
Root Cause
   ↓
Remediation Plan
   ↓
Policy / Permission / Approval
   ↓
Execute
   ↓
Validate
```

# 3. 总体架构

```text
                 Monitoring / User Request
                         │
                         ▼
                    LangGraph
                         │
                         ▼
                Investigation Agent
                         │
                  Initial Planning
                         │
                         ▼
                 Investigation Plan
                         │
                         ▼
                    Task Execution
                         │
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
   Knowledge          Fact          Evidence Tools
   Retrieval         Retrieval
        │                │                │
        ▼                ▼                ▼
  KnowledgeHit       FactResult    ComputationResult
        │                │                │
        └────────────────┼────────────────┘
                         ▼
                    Observation
                         │
                         ▼
               Investigation State
                         │
                         ▼
                    Replanning
                         │
                ┌────────┴────────┐
                │                 │
             继续调查          调查充分
                │                 │
                ▼                 ▼
          新一轮 Tasks      Evidence Qualification
                                  │
                                  ▼
                             Evidence Store
                                  │
                                  ▼
                              Policy Gate
                                  │
                    SUPPORTED / REJECTED /
                       INCONCLUSIVE
                                  │
                           SUPPORTED Root Cause
                                  │
                                  ▼
                         Remediation Planning
                                  │
                                  ▼
                  Policy / Permission / Approval
                                  │
                                  ▼
                            Action Executor
                                  │
                                  ▼
                        Independent Validation
                                  │
                                  ▼
                    Release / Observe / Rollback
                                  │
                                  ▼
                            Knowledge Update
```

上图的三类 Capability 表示完整调查循环的执行能力，不表示 Initial Planning 会同时生成
三类任务。第一轮只进入 Knowledge Retrieval；Fact Retrieval 与 Evidence Computation 必须由
取得 Observation 后的 Replanning 规划。

# 4. Investigation Agent

V1 不强制采用多 Agent。

系统只保留一个核心智能角色：

> **Investigation Agent**

其职责为：

- 根据用户请求或监控告警生成第一轮知识检索计划；
- 在获得 Observation 后形成和维护候选 Hypothesis；
- 根据 Investigation State 选择后续调查方向并进行 Replanning；
- 判断是否还需要继续调查；
- 在调查结束时提交给 Policy Gate，而不是自行确认根因。

Initial Planning 与 Replanning 的职责不同：

- Initial Planning 只定义第一轮需要获取哪些历史知识，不产生 Hypothesis；
- Replanning 才能根据已经取得的 Observation 形成 Hypothesis，并规划事实检索或证据计算。

Investigation Agent 不负责：

- 直接计算 KS、PSI；
- 直接访问生产数据库；
- 直接执行 SQL / Shell；
- 直接修改模型、特征或线上配置；
- 自行决定最终 `SUPPORTED`；
- 绕过 Policy Gate。

V1 暂不拆分 Knowledge Agent、Fact Agent、Data Agent、Feature Agent 等子 Agent。

只有当某个能力自身形成独立目标、独立状态和多步规划需求时，才考虑升级为子 Agent。

# 5. Initial Planning

调查开始后，Investigation Agent 首先生成 `InvestigationPlan`。当前输入由
`InitialPlanningInput` 统一承载，来源必须且只能有一个：

```text
InitialPlanningInput
├── user_query?              ─┐
├── monitoring_alert?        ─┴─ XOR
└── available_capabilities[]
```

用户请求的最小结构为：

```text
UserQuery
├── query
├── model_id?
└── time_window?
```

监控告警的最小结构为：

```text
MonitoringAlert
├── alert_id
├── model_id
├── metric
├── symptom
├── time_window
├── current_value
├── reference_value
└── triggered_at
```

Initial Planning 输出的正式业务对象为：

```text
InvestigationPlan
├── goal
└── tasks[]
```

第一轮任务全部为 `KNOWLEDGE_RETRIEVAL`，用于获取形成后续判断所需的历史背景。例如：

```text
Goal:
建立 credit_v3 KS 下降异常的初始调查背景

Tasks:
T1 搜索历史类似 KS 下降案例及调查过程
T2 获取 credit_v3 的模型背景、已知限制和历史问题
```

LLM 输出不得直接反序列化为正式 `InvestigationTask`。Agent 内部使用严格的 Draft Schema：

```text
_InitialPlanDraft
├── goal
└── tasks[]
    ├── objective
    └── expected_result
```

Draft 禁止额外字段。系统将 Draft 转换为正式任务，并负责设置：

```text
task_id = UUID
capability = KNOWLEDGE_RETRIEVAL
status = PENDING
```

因此 Initial Planning 只负责：

> **定义第一轮需要获取什么历史知识。**

不直接给出根因、Hypothesis、事实判断或计算结果。

# 6. Investigation Task 与 Capability

每个调查任务统一表示为：

```text
InvestigationTask
├── task_id
├── objective
├── capability
├── expected_result
└── status
```

其中 Agent 规划 `objective` 与 `expected_result`；`task_id`、`capability` 和 `status`
由系统在 Draft 转正式任务时设置。当前未确认 `candidate_tools`、`depends_on`、`priority`
等字段，不进入当前契约。

V1 任务可选择三类执行能力。

阶段约束：

- Initial Planning 只生成 `KNOWLEDGE_RETRIEVAL`；
- Replanning 在已有 Observation 后，才可以规划 `FACT_RETRIEVAL` 或 `COMPUTATION`；
- Capability 是任务执行语义，不代表独立子 Agent。

## 6.1 Knowledge Retrieval

用于回答：

> 历史上知道什么？

典型场景：

- 搜索历史故障 Case；
- 查找类似异常；
- 获取模型开发背景；
- 获取已知局限；
- 获取历史调查经验。

统一入口：

```text
knowledge_search(query, filters, top_k)
```

输出：

```text
KnowledgeHit[]
```

## 6.2 Fact Retrieval

用于回答：

> 系统已经知道的确定事实是什么？

典型场景：

- 模型版本；
- 模型适用范围；
- 已登记变更；
- 文档明确章节；
- 已持久化的监控结果；
- 发布记录。

典型工具：

```text
get_model_metadata()
get_document_section()
get_change_record()
get_metric_snapshot()
```

原则：

> 已经存在答案 → Retrieve。

## 6.3 Evidence Computation

用于回答：

> 需要基于当前数据重新计算什么，才能验证 Hypothesis？

典型工具：

```text
check_label_maturity()
compute_model_ks()
compute_auc()
compute_feature_psi()
compare_channel_mix()
compute_segment_performance()
check_missing_rate()
check_feature_coverage()
compare_score_distribution()
```

原则：

> 需要基于当前数据推导新的事实 → Compute。

# 7. Task Execution

Investigation Agent 不直接访问底层系统。

当前代码中的 `task_execution` 仍是 V0 placeholder，尚未实现 Capability Router、Tool
Registry 或任何真实工具调用。以下内容是下一阶段的目标边界，而不是已实现功能。

Task Execution 通过 Tool Registry 执行。

流程：

```text
InvestigationTask
      ↓
Capability Router
      ↓
Tool Registry
      ↓
Schema Validation
      ↓
Permission Check
      ↓
Tool Execute
      ↓
Tool Result
```

Tool 必须声明：

```text
tool_name
capability
input_schema
output_schema
permission_level
timeout
idempotency
audit_tags
```

V1 不允许 Agent 自由拼接 SQL、Shell 或任意代码。

# 8. Observation 与 Investigation State

Task Execution 后的结果不统一直接叫 Evidence。

当前代码中的 `observation` 仍是 V0 placeholder，尚未定义正式 Observation 契约。实现时
必须保持工具原始结果、Observation 与正式 Evidence 三种语义分离。

不同能力输出保持不同语义：

```text
Knowledge Retrieval
→ KnowledgeHit

Fact Retrieval
→ FactResult

Evidence Computation
→ ComputationResult
```

这些结果统一进入：

> **Observation**

Observation 表示：

> **本轮工具执行后系统观察到了什么。**

统一结构可定义为：

```text
Observation
├── observation_id
├── task_id
├── result_type
├── producer
├── payload
├── source_refs[]
├── created_at
└── status
```

其中：

```text
result_type =
KNOWLEDGE
FACT
COMPUTATION
```

当前 V0 已实现的最小 `InvestigationState` 为：

```text
InvestigationState
├── planning_input
├── investigation_plan?
└── investigation_complete?
```

后续实现 Task Execution、Observation 和 Replanning 时，再依据已经确认的跨节点数据扩展
State；Graph 节点阶段不重复保存为 State 字段。

# 9. Replanning

Replanning 是本系统使用 ReAct 思想的核心位置。

当前代码中的 `replanning` 仍是结束 V0 骨架的 placeholder。目标流程中，第一次 Replanning
发生在知识检索 Observation 产生之后；Hypothesis 从这里开始形成，而不是由 Initial Planning
预先生成。

它的输入不是单独的 Evidence，而是完整：

> **Investigation State**

包括：

- 当前 Plan；
- 已有 Hypothesis（第一轮 Replanning 前可以为空）；
- KnowledgeHit；
- FactResult；
- ComputationResult；
- 已完成任务；
- 未完成任务；
- Tool Failure；
- 当前成本和轮次。

Replanning 可以：

- 根据 KnowledgeHit 或其他 Observation 形成 Hypothesis；
- 调整已有 Hypothesis 的调查顺序；
- 删除已被否决的方向；
- 增加新的 Hypothesis；
- 追加新的调查任务；
- 判断是否已有足够信息进入 Evidence Qualification；
- 判断是否需要人工接管。

示例：

```text
第一轮 Observation：

历史相似 Case 显示：KS 下降常与标签成熟度、客群迁移和版本变更有关
模型开发报告显示：credit_v3 对 Channel C 的稳定性有限

Replanning：

形成 H1：当前评估样本标签可能未成熟
形成 H2：近期客群结构可能发生变化
形成 H3：模型或特征版本可能发生变化

新增：
T3 检索当前模型与特征版本（FACT_RETRIEVAL）
T4 计算标签成熟状态（COMPUTATION）
T5 比较渠道与客群结构（COMPUTATION）
```

因此 Replanning 回答的是：

> **下一步还应该查什么？**

# 10. Evidence Qualification 与 Evidence Store

Observation 不等于 Evidence。

Evidence Qualification 用于判断：

> **哪些 Observation 可以正式用于支持或否决某个 Hypothesis。**

例如：

```text
KnowledgeHit:
历史 Case 中 Population Shift 曾导致 KS 下降
```

它适合：

```text
生成 / 提升 H3 Population Shift
```

但不能单独确认当前 Root Cause。

而：

```text
ComputationResult:
Channel C 12% → 37%
Channel C KS 0.34 → 0.20
```

可以转换为：

```text
Supporting Evidence
```

统一 Evidence 结构：

```text
EvidenceRecord
├── evidence_id
├── hypothesis_id
├── source_observation_id
├── evidence_type
├── direction
├── strength
├── result
├── source_refs[]
├── dataset_version
├── created_at
└── checksum
```

其中：

```text
direction =
SUPPORT
CONTRADICT
NEUTRAL
```

Evidence Store 只保存正式用于根因判断的结果。

# 11. Policy Gate 与根因判定

Policy Gate 不属于 Agent 可自由选择的 Capability。

它是 LangGraph 中的强制节点。

输入：

```text
Hypothesis
+
Qualified Evidence
+
Policy Rules
```

输出：

```text
SUPPORTED
REJECTED
INCONCLUSIVE
```

示例规则：

```text
IF label_maturity < threshold
THEN INCONCLUSIVE

IF sample_count < min_sample_size
THEN INCONCLUSIVE

IF required_supporting_evidence satisfied
AND no blocking contradiction
THEN SUPPORTED

IF decisive contradiction exists
THEN REJECTED
```

关键原则：

> Agent Reasoning ≠ Business Decision

Investigation Agent 可以认为“某个原因可能性很高”，但是否进入 `SUPPORTED` 必须由 Policy Gate 决定。

# 12. 处置与验证闭环

只有 `SUPPORTED Root Cause` 才能进入处置阶段。

处置阶段不采用自由 ReAct。

流程：

```text
SUPPORTED Root Cause
        ↓
Remediation Planning
        ↓
RemediationPlan
        ↓
Policy Gate
        ↓
Permission Check
        ↓
Human Approval（按风险）
        ↓
Action Executor
        ↓
Independent Validation
        ↓
Release / Observation
        ↓
Promote / Rollback
```

Action Tool 示例：

```text
rerun_etl_job()
backfill_data()
train_candidate_model()
deploy_feature_pipeline()
rollback_release()
start_gray_release()
promote_release()
```

所有状态变更动作必须具备：

```text
action_id
idempotency_key
precondition
permission_level
approval_required
rollback_handle
audit_record
```

# 13. 知识库设计

V1 只保留两类核心文档。

## 13.1 模型开发报告

主要回答：

- 模型是什么；
- 为什么开发；
- 使用什么数据；
- 特征如何构造；
- 模型结构是什么；
- 开发阶段表现如何；
- 适用范围和局限是什么。

## 13.2 模型故障 / 根因调查报告

主要回答：

- 出现过什么异常；
- 调查过哪些 Hypothesis；
- 哪些原因被否决；
- 使用了哪些证据；
- 最终根因是什么；
- 如何处置；
- 验证结果如何；
- 有哪些可复用经验。

V1 检索策略：

```text
Metadata Filter
+
Section-aware Chunking
+
Vector Retrieval
+
Full-text / Exact Lookup
```

暂不将 GraphRAG / Neo4j 作为前置依赖。

# 14. LangGraph 状态与核心数据对象

## 14.1 推荐状态

```text
OPEN
  ↓
PLANNING
  ↓
INVESTIGATING
  ↓
REPLANNING
  ├──► INVESTIGATING
  └──► EVIDENCE_QUALIFYING
              ↓
          EVALUATING
      ┌───────┼────────┐
      ▼       ▼        ▼
 SUPPORTED REJECTED INCONCLUSIVE
      │                 │
      ▼                 ▼
REMEDIATION_PLAN    HUMAN_HANDOFF
      ↓
AWAITING_APPROVAL
      ↓
EXECUTING
      ↓
VALIDATING
      ↓
OBSERVING
   ┌────┴────┐
   ▼         ▼
 CLOSED   ROLLED_BACK
```

## 14.2 核心领域对象

```text
InvestigationContext
InvestigationPlan
InvestigationTask
Hypothesis
Observation
EvidenceRecord
PolicyDecision
RemediationPlan
ActionExecution
ValidationResult
KnowledgeDocument
```

这些对象构成系统稳定接口。

具体 Agent、RAG、Tool 和 Rule Engine 均依赖这些数据契约，而不是彼此直接耦合。

# 15. V1 开发边界与扩展接口

V1 优先实现：

1. LangGraph 主状态机；
2. Investigation Agent；
3. Initial Planning；
4. Task Execution；
5. 三类 Capability；
6. Tool Registry；
7. Observation；
8. Investigation State；
9. Replanning；
10. Evidence Qualification；
11. Evidence Store；
12. Policy Gate；
13. RemediationPlan；
14. Action Executor 抽象；
15. Validation 流程；
16. 两类知识文档摄入与 RAG；
17. checkpoint、interrupt、resume、audit。

V1 暂不优先实现：

- 多 Agent 协作框架；
- Knowledge Agent；
- Data Agent；
- Feature Agent；
- Validation Agent；
- GraphRAG / Neo4j；
- Agent 任意 SQL；
- Agent 任意代码执行；
- 无审批高风险生产变更。

# 16. 子 Agent 扩展接口

当前架构不依赖子 Agent，但为未来保留升级路径。

当某个能力满足以下条件时，可升级为 Sub-Agent：

```text
有独立目标
+
有独立状态
+
需要多步规划
+
需要调用多个 Tool
+
需要根据中间结果继续 Replanning
```

例如未来可能出现：

```text
Data Investigation Agent
Feature Investigation Agent
Knowledge Research Agent
Remediation Agent
```

升级后仍必须通过统一：

```text
InvestigationTask
Observation
EvidenceRecord
PolicyDecision
```

与主工作流协作。

因此系统是：

> **Agent-ready，但不是 Multi-Agent-first。**

# 17. 最终架构结论

V1 的核心不是 RAG，也不是多 Agent，而是：

> **Investigation Workflow**

整体逻辑为：

```text
Initial Planning
      ↓
Task Execution
      ↓
Observation
      ↓
Investigation State
      ↓
Replanning
      ↓
Evidence Qualification
      ↓
Policy Gate
      ↓
SUPPORTED Root Cause
      ↓
Remediation Plan
      ↓
Gate
      ↓
Execute
      ↓
Validate
```

其中：

- **RAG** 负责提供历史知识；
- **Fact Retrieval** 负责提供确定事实；
- **Evidence Tools** 负责计算当前调查结果；
- **Observation** 负责承接所有工具输出；
- **Replanning** 负责决定下一步查什么；
- **Evidence Qualification** 负责判断哪些 Observation 可用于根因判定；
- **Policy Gate** 负责确定性确认或否决根因；
- **Action Executor** 负责受控执行处置；
- **Validation** 负责验证修复效果。

这套结构是后续所有 Agent、Tool、Knowledge、Rule 和 Action 模块设计的总体基线。
