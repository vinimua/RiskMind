"""模型边界替身，只支持当前 LangChain 消息契约。"""
from langchain_core.messages import AIMessage


def tool_response(name="knowledge_search", arguments=None, *, finish="tool_calls"):
    return AIMessage(content="", response_metadata={"finish_reason": finish}, tool_calls=[{
        "name": name, "args": arguments if arguments is not None else {"query": "history", "top_k": 1},
        "id": "call-test", "type": "tool_call",
    }])


class ToolModelStub:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def bind_tools(self, tools, **options):
        self.binding = dict(tools=tools, **options)
        return self

    def invoke(self, messages, **options):
        self.calls.append(dict(messages=messages, **getattr(self, "binding", {}), **options))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


    async def ainvoke(self, messages, **options):
        return self.invoke(messages, **options)
