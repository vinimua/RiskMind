"""个人文档的模型边界：目录不含正文、按需读取及读取次数约束。"""
import asyncio
import json
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import ValidationError
from risk_platform.conversations.agent import ChatAgent
from risk_platform.conversations.contracts import ChatDraft
from risk_platform.memory.personal_contracts import PersonalNote, NoteChange
from tool_model_helpers import ToolModelStub, tool_response


class SequenceModel(ToolModelStub):
    def __init__(self, replies):
        super().__init__(None)
        self.replies = iter(replies)

    async def ainvoke(self, messages, **options):
        self.response = next(self.replies)
        return self.invoke(list(messages), **options)


def answer():
    return AIMessage(content='{"answer":"ok","memory_candidate":null}', response_metadata={"finish_reason":"stop"})


NOTE = PersonalNote(note_id="N", version=1, title="讲解偏好", description="解释代码时使用", content="PRIVATE_BODY")
INDEX = [NOTE.model_dump(exclude={"content"})]


def test_directory_then_read_body_then_answer():
    model = SequenceModel([tool_response("read_memory", {"note_id":"N"}), answer()])
    reads = []
    async def read(note_id):
        reads.append(note_id)
        return NOTE
    result = asyncio.run(ChatAgent("test", tool_model=model).decide("解释", history=[], memory_index=INDEX, read_memory=read))
    assert result.answer == "ok" and reads == ["N"]
    assert "PRIVATE_BODY" not in str(model.calls[0]["messages"])
    assert any(isinstance(m, ToolMessage) and "PRIVATE_BODY" in m.content for m in model.calls[1]["messages"])
    schemas = [t["function"]["parameters"] for t in model.calls[0]["tools"] if t["function"]["name"]=="read_memory"]
    assert set(schemas[0]["properties"]) == {"note_id", "start_paragraph"}


@pytest.mark.parametrize("kind", ["unoffered", "changed", "repeat"])
def test_read_is_restricted(kind):
    request = tool_response("read_memory", {"note_id":"other" if kind=="unoffered" else "N"})
    model = SequenceModel([request, request, answer()])
    async def read(note_id):
        return NOTE.model_copy(update={"version":2}) if kind=="changed" else NOTE
    with pytest.raises(ValueError):
        asyncio.run(ChatAgent("test",tool_model=model).decide("read",history=[],memory_index=INDEX,read_memory=read))


def test_old_payload_is_rejected_and_changes_require_read_versions():
    with pytest.raises(ValidationError):
        ChatDraft(answer="ok", memory={})
    for value in [dict(action="UPDATE"), dict(action="NONE",note_id="N"), dict(action="CREATE")]:
        with pytest.raises(ValidationError):
            NoteChange(**value)


def test_maintenance_reads_existing_note_before_proposing_update():
    change = dict(action="UPDATE",note_id="N",expected_version=1,
                  document=dict(title="讲解偏好",description="解释代码时使用",content="更新正文"))
    model=SequenceModel([tool_response("read_memory", {"note_id":"N"}),
        AIMessage(content=json.dumps(change),response_metadata={"finish_reason":"stop"})])
    async def read(note_id): return NOTE
    assert asyncio.run(ChatAgent("test",tool_model=model).maintain("补充偏好",index=INDEX,read_memory=read)).expected_version==1
