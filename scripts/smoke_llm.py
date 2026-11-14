"""Real-provider probes using the production Agent/parser. No case/database writes.

Run from repository root after installing backend: python scripts/smoke_llm.py
"""
import sys
import os
import re
import json
from email.utils import parsedate_to_datetime
from datetime import timezone
from risk_platform.core.llm import create_llm_agents
from risk_platform.conversations.contracts import ChatDraft, ToolInvocation
from risk_platform.memory.contracts import AgentContext, MemoryScope


def run_probes(chat, *, on_stage=lambda stage: None):
    context = AgentContext(scope=MemoryScope(kind="CONVERSATION", id="smoke-local"))
    on_stage("structured_answer")
    answer = chat.decide("Explain PSI briefly. Return JSON answer and empty memory.",
                         history=[], memory_context=context, tool_names=())
    if not isinstance(answer, ChatDraft):
        raise ValueError("Expected structured answer")
    print("structured_answer: PASS")
    on_stage("native_tool_call")
    proposal = chat.decide("Read the current progress of case smoke-case-001 using the progress tool.",
        history=[], memory_context=context, tool_names=("get_investigation_progress",),
        investigation_id="smoke-case-001")
    if (not isinstance(proposal, ToolInvocation)
            or proposal.name != "get_investigation_progress"
            or proposal.arguments.investigation_id != "smoke-case-001"):
        raise ValueError("Expected native progress call with exact target")
    print("native_tool_call: PASS (not executed)")


def failure_diagnostic(error, stage):
    """Allowlisted metadata only; never stringify exceptions or response bodies."""
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    nested = body.get("error") if isinstance(body, dict) else None
    code = (nested.get("code") if isinstance(nested, dict) else None)
    if code is None and isinstance(body, dict):
        code = body.get("code")
    if code is None:
        code = getattr(error, "code", None)
    if type(code) is int:
        code = str(code)
    # Unknown/free-form values are not safe diagnostics. No message/body printing.
    secrets = [os.environ.get(name, "").strip() for name in ("DEEPSEEK_API_KEY", "MODELSCOPE_API_KEY", "ZHIPU_API_KEY")]
    if (not isinstance(code, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code) is None
            or code.lower().startswith(("ms-", "sk-"))
            or any(secret and secret in code for secret in secrets)):
        code = None
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry = headers.get("retry-after")
    retry_seconds, retry_at = None, None
    if isinstance(retry, str):
        if re.fullmatch(r"[0-9]{1,9}", retry.strip()):
            retry_seconds = int(retry.strip())
        else:
            try:
                date = parsedate_to_datetime(retry)
                if date.tzinfo is not None:
                    retry_at = date.astimezone(timezone.utc).isoformat()
            except (TypeError, ValueError, OverflowError):
                pass
    return {"stage": stage, "exception_type": type(error).__name__,
            "http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "provider_error_code": code, "retry_after_seconds": retry_seconds,
            "retry_after_utc": retry_at}


def report_failure(error, stage):
    print("model_probe: FAIL " + json.dumps(failure_diagnostic(error, stage), ensure_ascii=True), file=sys.stderr)


def main():
    chat = None
    stage = "configuration"
    outcome = 0
    def mark_stage(value):
        nonlocal stage
        stage = value
    try:
        chat, _ = create_llm_agents()
        run_probes(chat, on_stage=mark_stage)
    except Exception as error:
        report_failure(error, stage)
        outcome = 1
    finally:
        if chat is not None:
            try:
                chat.client.close()
            except Exception as error:
                report_failure(error, "client_cleanup")
                outcome = 1
    return outcome


if __name__ == "__main__":
    raise SystemExit(main())
