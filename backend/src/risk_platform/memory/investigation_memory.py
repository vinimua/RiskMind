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
    # 1. allowed 是这份解释允许引用的执行 ID 集合；set 便于判断包含关系。
    allowed = set(observation.execution_refs)
    # *元组：展开已有元素，把三组内容合并成一个元组，不是在这里调用函数。
    notes = (*observation.findings, *observation.knowledge_gaps, *observation.next_step_suggestions)
    # 集合 A <= B 表示 A 是 B 的子集。只要有一条内容引用了 allowed 以外的执行，就拒绝。
    # 这是引用范围检查，不证明引文支持结论，也不替代来源落库、案件归属和权限检查。
    if any(not set(note.execution_refs) <= allowed for note in notes):
        raise ValueError("记忆引用不属于源 Observation")
    # 2. 每条执行摘要转成一条字符串；s 是一份 ExecutionObservation。
    # f-string 将字段插入正文；status.value 取枚举的字符串值，如 FAILED。
    # status 与 outcome 原样保留：失败、拒绝、成功但零命中不能被合并成同一状态。
    # tuple(生成器) 生成不可变序列；此处不会再次执行任务或查询工具。
    summaries = tuple(f"execution={s.execution_id}; task={s.task_id}; status={s.status.value}; outcome={s.outcome}"
                      for s in observation.execution_summaries)
    # 3. f 是一条历史发现，statement 是解释正文，limitation 是其适用限制。
    # 两者合成同一条内容；组装器以后要么保留整条，要么整条省略，不截成半句。
    # 这里只转录来源解释，不把历史线索升级为当前案件已确认根因。
    findings = tuple(f"{f.statement}\n限制：{f.limitation}" for f in observation.findings)
    # 4. source_result_id 先编码成字节，再计算 SHA-256，hexdigest() 得到十六进制字符串。
    # 同一来源得到同一 memory_id；这只是稳定身份，不是加密，也不单独保证写入幂等。
    # 仓库中的来源比较、事务锁和数据库唯一约束负责实际的重复写入控制。
    return MemoryRecord(memory_id="memory-" + hashlib.sha256(source_result_id.encode()).hexdigest(),
        # 归属和来源各保存一次：scope 指案件，source_id 指可回查的账本 Result。
        scope=MemoryScope(kind="INVESTIGATION", id=investigation_id),
        source_type="OBSERVATION_RESULT", source_id=source_result_id,
        content=MemoryContent(execution_summaries=summaries, historical_findings=findings,
            # 5. 缺口放前面，建议放后面，统一作为后续事项；不自动创建正式任务。
            # dict.fromkeys 按首次出现顺序去除完全相同的字符串，最后取键转成 tuple。
            # 例如“补查版本”重复两次只留一次；“版本是否变化”与“补查版本”不会语义合并。
            # user_preferences 和 background 未由此函数提取，使用 MemoryContent 的空元组默认值。
            follow_ups=tuple(dict.fromkeys(n.text for n in (
                *observation.knowledge_gaps, *observation.next_step_suggestions)))))
