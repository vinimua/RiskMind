# DevF 记忆系统代码参考

本文整理本地 DevF 源码的文件入口、关键代码和阅读路线，供对照 RiskMind 使用。没有修改或复制 DevF 项目到业务目录，也没有运行 DevF 测试。下文标有“简化”的代码省略了无关细节，不是可直接替换的完整实现。行号对应整理时的源码，后续修改后可能变化。

## 1. 先看整体调用链

```text
api/routes/chat.py
  → ProgressiveContextManager.ensure_headroom()  为新消息预留空间
  → _prepare_chat_evidence()
      → MessageStore.append()                  保存用户消息
      → EvidenceStore.rebuild_repo()           更新资料检索投影
      → commit
      → ContextAssembler.assemble()            记忆、历史、资料分别组装
  → 组织 server_context / server_messages / evidence
  → _build_conversation_tools()                绑定可执行工具
  → ChatAgent                                 模型推理及工具调用
  → SessionSealer.seal()                       保存封存现场、提取与合并记忆
  → 上层调用方最终提交
```

聊天有多种路由入口，不能把这条概括当成每条路由的逐行实现。先看 [chat.py](../DevF/backend/app/api/routes/chat.py) 的 `_prepare_chat_evidence`（1592）、`_conversation_plan_execute_sse`（1605 附近）及后续聊天路由（1730 起）。

## 2. 数据结构：原文、摘要、长期候选分开

源文件：[db/models.py](../DevF/backend/app/db/models.py)。

| 类 / 起始行 | 保存内容 | 阅读重点 |
| --- | --- | --- |
| ChatMessage / 304 | 用户与助手消息及元信息 | 原文身份、会话关联 |
| ChatSession / 319 | 封存的一轮对话、工具事件、摘要和状态 | transcript_json 与 digest 不同 |
| ThreadMemory / 335 | 仓库范围工作记忆 | repo_id 与会话隔离的差别 |
| ConversationMemory / 352 | 当前会话累计工作记忆 | conversation_id 唯一，持续合并更新 |
| RecallEvent / 400 | 记忆或资料召回审计 | 谁通过哪个入口召回什么 |
| MemoryCandidate / 419 | 可转为长期知识的候选 | pending / approved / rejected 生命周期 |

ConversationMemory 的核心字段摘录：

```python
summary: Mapped[str] = mapped_column(Text, default="")
facts: Mapped[list[str]] = mapped_column(JSON, default=list)
decisions: Mapped[list[str]] = mapped_column(JSON, default=list)
open_questions: Mapped[list[str]] = mapped_column(JSON, default=list)
tasks: Mapped[list[str]] = mapped_column(JSON, default=list)
user_preferences: Mapped[list[str]] = mapped_column(JSON, default=list)
repo_context: Mapped[list[str]] = mapped_column(JSON, default=list)
citations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
```

这里的 facts 是提取结果字段名，不代表系统已经证明所有内容为真。DevF 工作记忆会合并更新；RiskMind 当前采用不可变的来源记忆和阶段快照，不能直接照搬覆盖方式。

## 3. MessageStore：保存和读取原始历史

源文件：[chat_memory.py](../DevF/backend/app/services/chat_memory.py)。

- `MessageStore`：78。
- `append()`：86，保存消息。
- `recent()`：117，近期消息。
- `timeline()`：187，历史时间线。
- `context_projection()`：272，将原消息及关联工具事件投影成上下文。
- `last_compact_boundary()`：371，定位最近压缩边界。

关键区别：数据库里的历史和本次模型看到的历史不是同一个集合。context_projection 可以展开工具调用和结果；压缩后的 Prompt 不应被误认为全量原文。

RiskMind 对应的是现有执行账本。未来聊天接入后再增加消息存储，不需要把调查任务伪装成 ChatMessage。

## 4. EvidenceStore：统一资料召回

同文件的 `EvidenceStore` 起于 386：

- `rebuild_repo()`：395，构建可检索资料投影，包括记忆等来源。
- `search()`：501，先处理 RAG 候选，再补本地词面检索，按来源类型及 ID 去重。
- `_add()`：581，写投影条目。

关键代码摘录：

```python
query_terms = _tokens(query)
if not query_terms:
    return []
results = []
seen = set()
```

后续 RAG 调用捕获 MilvusUnavailableError 时退回本地路径。因此这里的空集合与降级需要结合上下文诊断理解，不能直接对应 RiskMind 的“检索成功但零命中”。EvidenceStore 中的 evidence 是聊天资料，不等同于本项目通过准入的正式 Evidence。

## 5. ContextAssembler：最值得先读

同文件 `AssembledContext`（588）、`ContextAssembler`（595）、`assemble()`（606）。

返回结构：

```python
class AssembledContext:
    system_context: str
    history_messages: list[dict[str, str]]
    evidence: list[dict[str, Any]]
    compression_stats: dict[str, Any] = field(default_factory=dict)
```

assemble 的步骤：

1. 读取最近压缩边界。
2. 按会话或仓库读取结构化记忆。
3. 读取上一段封存摘要。
4. 用当前问题召回资料。
5. 读取近期消息并展开工具事件。
6. 判断新用户消息是否已经写进历史，避免重复计数。
7. 依据压力执行微压缩，再按近期历史预算裁剪。
8. 把稳定背景、记忆、资料分别放进各自预算。
9. 单独返回 history_messages，不在背景里重复嵌入历史。
10. 返回压缩、截断与历史覆盖诊断。

注意 system_context 中也包含历史内容，不应因为字段名带 system 就认为其中每句话都是可信系统规则。

RiskMind 对应 [memory/context.py](../backend/src/risk_platform/memory/context.py)。目前仅实现案件记忆组装和字符预算，没有实现 DevF 的全部历史投影和压缩管理。

## 6. ProgressiveContextManager：逐步压缩

同文件起于 795：

| 函数 / 行 | 职责 |
| --- | --- |
| ensure_headroom / 806 | 依据当前上下文压力决定是否需要处理 |
| _compact / 887 | 执行压缩，保存边界等信息 |
| _compaction_plan_for_stage / 1010 | 不同压力阶段采用不同处理方案 |
| _messages_to_keep / 1030 | 保留近期消息 |
| _summary_for / 1051 | 获取压缩摘要 |
| _insert_boundary / 1088 | 保存摘要边界和回查信息 |
| _rehydration_state / 1163 | 提取恢复工作状态 |
| _record_failure / 1253 | 保存压缩失败记录 |

算法函数集中在 [context_compression.py](../DevF/backend/app/services/context_compression.py)：

- `estimate_tokens()`：85，估算而非精确 tokenizer。
- `calculate_context_pressure()`：223，计算压力。
- `adjust_keep_start_to_preserve_api_invariants()`：339，裁剪时保护工具调用与结果的关联。
- `ContextBudget` / `default_context_budget()`：432 / 451，分配预算。
- `compress_messages()`：473，压缩历史消息。
- `microcompact_messages()`：571，微压缩。
- `compress_retrieval_results()`：688，资料压缩。
- `ensure_compaction_summary_preserves_key_facts()`：817，检查关键内容保留。

这里尤其值得学习：不能随意保留 tool_result 却删除对应 tool_call，否则可能破坏模型 API 消息协议。关键内容保留检查也不等于证明自然语言摘要语义完全正确。

## 7. SessionSealer：保存现场并更新工作记忆

同文件起于 1272，`seal()` 在 1282，`_build_memory_update()` 在 1390，`_update_thread_memory()` 在 1485。

流程简化如下：

```python
fallback_update = fallback_memory_update(...)
# 新建 ChatSession，保存原文、工具事件和诊断
self.db.add(session)
self.db.flush()
try:
    memory_update = await self._build_memory_update(...)
except Exception:
    memory_update = fallback_update
try:
    with self.db.begin_nested():
        self._update_thread_memory(...)
        capture_memory_candidates(...)
        self.db.flush()
except Exception:
    # 保留封存现场，并记录警告
    ...
```

三个必须看清的细节：

- flush 不是 commit。transcript 中的 raw_persisted 表示处理阶段，不保证已经独立可靠提交；最终提交由调用方负责。
- begin_nested 是保存点，不是另一份独立持久化事务。内部增强失败可以回滚保存点，外层仍需提交。
- 提取失败可以保留 fallback 并标记 sealed_with_warnings，避免记忆增强失败抹掉已完成聊天。

与 RiskMind 的区别：当前确定性提取与 Observation 原子提交，失败则该提交事务回滚。未来 LLM 摘要应作为独立派生阶段，不照搬“事务中等待模型”的方式。

## 8. 结构化提取与合并算法

源文件：[context_compression.py](../DevF/backend/app/services/context_compression.py)。

- `structured_memory_from_model()`：901，将模型结果整理为 summary / facts / decisions / open_questions / tasks 等字段。
- `fallback_memory_update()`：914，模型不可用时的规则提取。
- `merge_memory()`：962，合并旧记忆与本次更新。
- `render_structured_memory()`：980，将记忆按预算渲染。
- `memory_document_text()`：1001，输出可索引文本。

先读字段规范化，再读合并与限长；不要仅看到字段是 list 就推断已解决冲突、事实纠错或语义验证。RiskMind 当前提取器对应 [investigation_memory.py](../backend/src/risk_platform/memory/investigation_memory.py)，没有实现 LLM 抽取或累计合并。

## 9. MemoryTools：摘要不够时回查原文

同文件起于 1520：

- `search_evidence()`：1531，按问题召回，返回结果、引用并记录召回事件。
- `get_thread_context()`：1562，精确读取消息或会话内容，支持字符分页。
- `read_session_events()`：1675，读取封存会话和工具事件。

offset / max_chars 是字符位置，不是 token 或字节。价值在于模型不必永久携带所有原文，需要时通过受控工具回查。

工具包装见 [chat_agent.py](../DevF/backend/app/services/agents/chat_agent.py) 的 `_build_langchain_tools()`（1429）；具体绑定见 [chat.py](../DevF/backend/app/api/routes/chat.py) 的 `_build_conversation_tools()`（1005）。

RiskMind 当前没有注册案件记忆回查工具；不能把未来能力当成已有功能。

## 10. memory_hub：长期记忆治理

源文件：[memory_hub.py](../DevF/backend/app/services/memory_hub.py)。

| 函数 / 行 | 职责 |
| --- | --- |
| record_recall_event / 24 | 记录召回行为 |
| memory_status / 71 | 汇总记忆状态 |
| capture_memory_candidates / 132 | 从记忆更新提取长期候选 |
| list_memory_candidates / 173 | 列出候选 |
| approve_memory_candidate / 191 | 候选转为 memory_note 文档 |
| reject_memory_candidate / 223 | 标记拒绝，不删除原文 |

审批核心简化：

```python
if candidate.status == "approved":
    return candidate, None

doc = add_document(..., commit=False)
candidate.status = "approved"
candidate.meta = {**(candidate.meta or {}), "approved_document_id": str(doc.id)}
db.flush()
```

这是顺序重复调用的防重复逻辑；不能仅凭这个 if 证明并发审批与 PG/向量库之间的跨系统原子性。权限入口、并发控制和外部失败还需分别审阅。

RiskMind 长期候选审核尚未实现。当前案件记忆不能自动成为跨案件可信经验。

## 11. 推荐阅读顺序

```text
models.py：认识原文、封存、工作记忆、候选四类记录
  → ContextAssembler：先理解模型最终收到什么
  → chat.py：上下文如何接进实际请求
  → SessionSealer：结果如何变成下一次的记忆
  → context_compression.py：提取、合并与压缩
  → MemoryTools：如何回查原文
  → memory_hub.py：经验如何进入长期知识
```

测试参考：[test_context_memory_projection.py](../DevF/backend/app/tests/test_context_memory_projection.py)、[test_context_compression.py](../DevF/backend/app/tests/test_context_compression.py)。本次只核对源码与文件链接，未执行这些测试，不据此宣称 DevF 的权限、并发或语义质量全部通过。
