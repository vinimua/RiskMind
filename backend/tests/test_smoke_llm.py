"""Offline provider failures: actionable metadata without raw credentials/payloads."""
import runpy
from pathlib import Path
from types import SimpleNamespace as NS
import httpx
from openai import RateLimitError
import pytest


@pytest.fixture
def smoke():
    return runpy.run_path(str(Path(__file__).resolve().parents[2]/"scripts"/"smoke_llm.py"))


def limited(body=None, retry=None):
    headers={} if retry is None else {"Retry-After":retry}
    response=httpx.Response(429,headers=headers,request=httpx.Request("POST","https://example.test/v1"))
    return RateLimitError("PRIVATE_EXCEPTION",response=response,body=body)


def test_rate_limit_metadata_without_private_payload(smoke,capsys):
    error=limited({"error":{"code":"insufficient_quota","message":"PRIVATE_BODY"}},"60")
    smoke["report_failure"](error,"structured_answer")
    output=capsys.readouterr().err
    assert '"http_status": 429' in output and '"retry_after_seconds": 60' in output
    assert "insufficient_quota" in output and "structured_answer" in output
    assert "PRIVATE" not in output


@pytest.mark.parametrize("code",["ms-private-token","sk-private-token","private-key","bad\nheader",{"secret":"x"}])
def test_unsafe_error_code_is_omitted(smoke,monkeypatch,code):
    monkeypatch.setenv("DEEPSEEK_API_KEY","private-key")
    data=smoke["failure_diagnostic"](limited({"code":code},"PRIVATE_HEADER"),"native_tool_call")
    assert data["provider_error_code"] is None
    assert data["retry_after_seconds"] is None and data["retry_after_utc"] is None


def test_http_date_and_absent_headers(smoke):
    data=smoke["failure_diagnostic"](limited({"code":1302},"Fri, 13 Nov 2026 10:00:00 GMT"),"native_tool_call")
    assert data["provider_error_code"]=="1302"
    assert data["retry_after_utc"]=="2026-11-13T10:00:00+00:00"
    absent=smoke["failure_diagnostic"](RuntimeError("PRIVATE"),"configuration")
    assert absent["http_status"] is None and absent["retry_after_seconds"] is None


def test_main_reports_correct_stage_closes_client_and_never_retries(smoke,monkeypatch,capsys):
    from risk_platform.core import llm
    from risk_platform.conversations.contracts import ChatDraft
    calls=[];closed=[]
    def decide(*args,**kwargs):
        calls.append(1)
        if len(calls)==1: return ChatDraft(answer="ok")
        raise limited({"error":{"code":"rate_limit_exceeded"}})
    chat=NS(decide=decide,client=NS(close=lambda:closed.append(1)))
    main=smoke["main"]
    monkeypatch.setitem(main.__globals__,"create_llm_agents",lambda:(chat,None))
    assert main()==1 and calls==[1,1] and closed==[1]
    output=capsys.readouterr()
    assert "structured_answer: PASS" in output.out
    assert '"stage": "native_tool_call"' in output.err and "PRIVATE" not in output.err
