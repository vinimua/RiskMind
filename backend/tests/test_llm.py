from types import SimpleNamespace
import pytest
from risk_platform.core import llm


def test_client_reads_deepseek_and_has_explicit_timeout_without_retries(monkeypatch):
    received = {}
    def fake(**kwargs):
        received.update(kwargs)
        return object()
    monkeypatch.setenv("DEEPSEEK_API_KEY", " test-key ")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://example.test/v1/")
    monkeypatch.setenv("MODELSCOPE_API_KEY", "unused-other-provider-key")
    monkeypatch.setattr(llm, "OpenAI", fake)
    assert llm.create_llm_client() is not None
    assert received == dict(api_key="test-key",base_url="https://example.test/v1/",timeout=120.0,max_retries=0)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_key_never_falls_back_to_modelscope(monkeypatch,value):
    monkeypatch.setenv("MODELSCOPE_API_KEY", "other-provider-key")
    if value is None: monkeypatch.delenv("DEEPSEEK_API_KEY",raising=False)
    else: monkeypatch.setenv("DEEPSEEK_API_KEY",value)
    with pytest.raises(RuntimeError,match="DEEPSEEK_API_KEY is not set"):
        llm.create_llm_client()


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_base_url_uses_deepseek_default(monkeypatch,value):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "key")
    if value is None: monkeypatch.delenv("DEEPSEEK_BASE_URL",raising=False)
    else: monkeypatch.setenv("DEEPSEEK_BASE_URL",value)
    monkeypatch.setattr(llm,"OpenAI",lambda **kwargs:kwargs)
    assert llm.create_llm_client()["base_url"] == llm.DEFAULT_BASE_URL


def test_agents_share_one_client_and_explicit_model(monkeypatch):
    calls=[];client=SimpleNamespace(api_key="test",base_url="https://example.test",chat=SimpleNamespace(completions=object()))
    monkeypatch.setenv("DEEPSEEK_MODEL"," deepseek-flash ")
    monkeypatch.setattr(llm,"create_llm_client",lambda:calls.append(1) or client)
    tool_model = object()
    monkeypatch.setattr(llm,"ChatOpenAI",lambda **kwargs:tool_model)
    chat,investigation=llm.create_llm_agents()
    assert investigation.client is client and calls==[1]
    assert chat.model==investigation.model=="deepseek-flash"
    assert chat.tool_model is investigation.tool_model is tool_model


@pytest.mark.parametrize("value", [None, "", "   "])
def test_model_is_required_before_creating_client(monkeypatch,value):
    if value is None: monkeypatch.delenv("DEEPSEEK_MODEL",raising=False)
    else: monkeypatch.setenv("DEEPSEEK_MODEL",value)
    def unexpected(): raise AssertionError("client should not be created")
    monkeypatch.setattr(llm,"create_llm_client",unexpected)
    with pytest.raises(RuntimeError,match="DEEPSEEK_MODEL is not set"):
        llm.create_llm_agents()


def test_smoke_probes_validate_both_production_response_types():
    import runpy
    from pathlib import Path
    from risk_platform.conversations.contracts import ChatDraft, ToolInvocation
    run=runpy.run_path(str(Path(__file__).resolve().parents[2]/"scripts"/"smoke_llm.py"))["run_probes"]
    calls=[]
    def decide(message, **kwargs):
        calls.append(kwargs)
        if len(calls)==1: return ChatDraft(answer="PSI compares distributions.")
        return ToolInvocation(call_id="test-call",name="get_investigation_progress",arguments={"investigation_id":"smoke-case-001"})
    run(SimpleNamespace(decide=decide))
    assert len(calls)==2 and calls[0]["tool_names"]==()
    assert calls[1]["investigation_id"]=="smoke-case-001"
    with pytest.raises(ValueError,match="Expected native progress"):
        run(SimpleNamespace(decide=lambda *args,**kwargs:ChatDraft(answer="not a tool")))
