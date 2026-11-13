import pytest
from pydantic import ValidationError
from risk_platform.conversations.contracts import ChatRequest, ToolInvocation, EntryConfirmation
from risk_platform.conversations.tools import validate_entry_target, EntryTargetError


def call(name="start_investigation",**arguments):
    return ToolInvocation(call_id="model-call",name=name,arguments=arguments)


@pytest.mark.parametrize("message,binding,tool",[
    ("调查 credit-v3",{},call(model_id="credit-v3")),
    ("调查模型credit-v3",{},call(model_id="credit-v3")),
    ("调查这个模型",{"model_id":"credit-v3"},call(model_id="credit-v3")),
    ("查询案件 I",{},call("get_investigation_progress",investigation_id="I")),
])
def test_literal_or_explicit_target_is_accepted(message,binding,tool):
    request=ChatRequest(request_id="q",message=message,**binding)
    assert validate_entry_target(request,tool)==tool


@pytest.mark.parametrize("message,binding,tool",[
    ("调查其他模型",{},call(model_id="credit-v3")),
    ("调查 credit-v30",{},call(model_id="credit-v3")),
    ("调查 B",{"model_id":"A"},call(model_id="B")),
    ("查案件 I",{"model_id":"M"},call("get_investigation_progress",investigation_id="I")),
    ("调查模型 M",{"investigation_id":"I"},call(model_id="M")),
])
def test_missing_substring_or_conflicting_target_requires_question(message,binding,tool):
    with pytest.raises(EntryTargetError):
        validate_entry_target(ChatRequest(request_id="q",message=message,**binding),tool)


def test_confirmation_requires_actual_boolean_and_cannot_change_target():
    for fields in [dict(confirmed="true"),dict(confirmed=True,model_id="another"),dict(confirmed=True,actor_id="admin")]:
        with pytest.raises(ValidationError):
            EntryConfirmation(invocation_id="call",decision_digest="a"*64,**fields)
