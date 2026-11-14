"""把已提交的 Observation 整理成分类记忆，不新增模型调用。

这里是确定性的数据转换：相同输入得到相同输出，不保存数据库、不执行鉴权。
调用方 capture_observation() 负责从正式结果读取来源，并在同一事务内保存记忆。
完整引文、执行关联仍保存在原账本，通过 source_id 回查，不逐条复制进记忆。
"""
import hashlib
from .contracts import MemoryContent, MemoryRecord, MemoryScope


def extract_memory(*, investigation_id, source_result_id, observation):
    """输入一份解释，返回一份 MemoryRecord。

    investigation_id：来源案件 ID。
    source_result_id：保存该 Observation 的账本 Result ID，不是工具 execution_id。
    observation：包含执行摘要、历史发现、知识缺口和下一步建议的解释对象。
    开头的 * 要求调用方使用参数名传值，减少几个身份参数被位置传反的风险。
    """
    # 新格式直接从来源核对执行；旧格式只读，不推导推进建议。
    if getattr(observation, "schema_version", None) == 2:
        allowed = set(observation.execution_refs)
        if any(s.execution_id not in allowed for f in observation.findings for s in f.sources):
            raise ValueError("记忆引用不属于源 Observation")
        findings = tuple(f.statement for f in observation.findings)
        follow_ups = observation.unresolved_questions
    else:
        findings = tuple(f"{f.statement}\n限制：{f.limitation}" for f in observation.findings)
        follow_ups = tuple(n.text for n in (*observation.knowledge_gaps, *observation.next_step_suggestions))
    summaries = tuple(f"execution={s.execution_id}; task={s.task_id}; status={s.status.value}; outcome={s.outcome}"
                      for s in observation.execution_summaries)
    return MemoryRecord(memory_id="memory-" + hashlib.sha256(source_result_id.encode()).hexdigest(),
        # 归属和来源各保存一次：scope 指案件，source_id 指可回查的账本 Result。
        scope=MemoryScope(kind="INVESTIGATION", id=investigation_id),
        source_type="OBSERVATION_RESULT", source_id=source_result_id,
        content=MemoryContent(execution_summaries=summaries, historical_findings=findings,
            follow_ups=tuple(dict.fromkeys(follow_ups))))
