import pytest
from pydantic import ValidationError
from risk_platform.conversations.contracts import ChatRequest, ChatResponse


def test_removed_action_and_conflicting_targets_are_rejected():
    for action in ("AUTO","CHAT","READ_INVESTIGATION","START_INVESTIGATION"):
        with pytest.raises(ValidationError):
            ChatRequest(request_id="Q",message="hi",action=action)
    with pytest.raises(ValidationError):
        ChatRequest(request_id="Q",message="hi",investigation_id="I",model_id="M")


def test_response_status_and_error_must_agree():
    for values in [dict(status="FAILED"),dict(status="SUCCEEDED",error_code="FORBIDDEN"),dict(status="FAILED",error_code="invented")]:
        with pytest.raises(ValidationError):
            ChatResponse(conversation_id="C",turn_id="T",answer="a",**values)
