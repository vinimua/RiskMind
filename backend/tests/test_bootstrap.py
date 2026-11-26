import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
import pytest
from fastapi.testclient import TestClient
from risk_platform import bootstrap
from risk_platform.workflow.runtime import InvestigationResource
from risk_platform.execution.contracts import KnowledgeExecutionContext


def settings():
    return bootstrap.LocalSettings(actor_id="local-user",api_token="test-token-"+"a"*32,
        allowed_model_ids=("M",),knowledge_profile="parent-child-v1")


def test_planning_capabilities_have_no_default():
    from risk_platform.entrypoints import EntryServices
    from risk_platform.workflow.contracts import UserQuery
    dependencies = dict(sessions=None, runner=None, chat_agent=None,
                        authorize_resource=lambda *_: True, authorize_conversation=lambda *_: True)
    source = dict(user_query=UserQuery(query="test", model_id="M"))
    services = EntryServices(**dependencies, tools_for=lambda *_: ())
    assert services._planning_source("actor", **source).available_capabilities == []
    services = EntryServices(**dependencies, tools_for=lambda *_: (NS(name="unregistered"),))
    with pytest.raises(ValueError, match="工具未注册"):
        services._planning_source("actor", **source)
    def denied(*_):
        raise PermissionError("denied")
    services = EntryServices(**dependencies, tools_for=denied)
    with pytest.raises(PermissionError, match="denied"):
        services._planning_source("actor", **source)


def configure(monkeypatch):
    for key,value in {"RISK_LOCAL_MODE":"1","RISK_LOCAL_ACTOR_ID":"local-user",
        "RISK_LOCAL_API_TOKEN":"test-token-"+"a"*32,"RISK_ALLOWED_MODEL_IDS":'["M"]',
        "RISK_KNOWLEDGE_PROFILE":"parent-child-v1"}.items(): monkeypatch.setenv(key,value)


def test_local_auth_and_grants_are_explicit():
    config=settings()
    token=config.api_token.get_secret_value()
    assert token not in repr(config)
    assert config.authenticate(NS(headers={"authorization":"Bearer "+token}))=="local-user"
    for header in ("", "Basic "+token, "Bearer wrong", "Bearer 非ASCII"):
        with pytest.raises(PermissionError): config.authenticate(NS(headers={"authorization":header}))
    assert config.authorize_resource("local-user","investigation:read",InvestigationResource("I","R","M"))
    assert not config.authorize_resource("other","investigation:read",InvestigationResource("I","R","M"))
    assert not config.authorize_resource("local-user","investigation:read",InvestigationResource("I","R","other"))
    assert not config.authorize_resource("local-user","release",InvestigationResource("I","R","M"))
    assert not config.authorize_conversation("local-user","chat:admin","C")
    good=KnowledgeExecutionContext(actor_id="local-user",index_profile="parent-child-v1",scope={"model_id":"M"},max_top_k=5)
    assert config.authorize_knowledge("local-user","knowledge:search",good)
    for changed in ({"actor_id":"other"},{"index_profile":"baseline-v1"},{"max_top_k":6}):
        assert not config.authorize_knowledge("local-user","knowledge:search",good.model_copy(update=changed))


def test_controlled_action_permissions_are_explicit_and_scoped(tmp_path):
    base = settings()
    resource = InvestigationResource("I", "R", "M")
    assert not base.authorize_resource("local-user", "action:approve", resource)
    configured = bootstrap.LocalSettings.model_validate(base.model_dump() | {"report_directory": tmp_path,
        "action_permissions": ("action:request", "action:read", "action:approve", "action:download")})
    assert configured.authorize_resource("local-user", "action:approve", resource)
    assert not configured.authorize_resource("local-user", "action:resolve", resource)
    assert not configured.authorize_resource("other", "action:approve", resource)
    assert not configured.authorize_resource("local-user", "action:approve", InvestigationResource("I", "R", "OTHER"))


def test_explicit_enable_and_required_configuration(monkeypatch):
    configure(monkeypatch)
    monkeypatch.delenv("RISK_LOCAL_MODE")
    with pytest.raises(RuntimeError,match="RISK_LOCAL_MODE"): bootstrap.create_local_app()
    configure(monkeypatch);monkeypatch.setenv("RISK_ALLOWED_MODEL_IDS","[]")
    with pytest.raises(ValueError): bootstrap.create_local_app()
    configure(monkeypatch);monkeypatch.setenv("RISK_LOCAL_API_TOKEN","short")
    with pytest.raises(ValueError): bootstrap.create_local_app()


def test_lifespan_installs_and_clears_dependencies(monkeypatch):
    import risk_platform.main as main
    configure(monkeypatch);events=[]
    monkeypatch.setattr(main,"shutdown_observability",lambda:events.append("observability-close"))
    @asynccontextmanager
    async def resources(config):
        events.append("open")
        yield NS()
        events.append("close")
    monkeypatch.setattr(bootstrap,"local_services",resources)
    app=bootstrap.create_local_app()
    assert app.state.entry_services is None
    with TestClient(app) as client:
        assert app.state.entry_services is not None and callable(app.state.authenticate)
        assert client.get("/health/live").status_code==200
    assert app.state.entry_services is None and app.state.authenticate is None
    assert events==["open","observability-close","close"]


@pytest.mark.parametrize("failure", ["startup", "shutdown"])
def test_lifespan_failure_cleans_resources_and_references(monkeypatch, failure):
    import risk_platform.main as main
    configure(monkeypatch)
    events=[]
    @asynccontextmanager
    async def resources(config):
        try:
            events.append("open")
            if failure == "startup":
                raise RuntimeError("startup failed")
            yield NS()
        finally:
            events.append("close")
    def shutdown():
        events.append("observability-close")
        raise RuntimeError("shutdown failed")
    monkeypatch.setattr(bootstrap,"local_services",resources)
    monkeypatch.setattr(main,"shutdown_observability",shutdown)
    app=bootstrap.create_local_app()
    assert events == []
    with pytest.raises(RuntimeError,match=failure + " failed"):
        with TestClient(app):
            assert app.state.entry_services is not None
    assert app.state.entry_services is None and app.state.authenticate is None
    assert events == (["open","close"] if failure == "startup" else
                      ["open","observability-close","close"])


def test_service_ownership_must_be_unambiguous():
    from risk_platform.main import create_app
    with pytest.raises(ValueError,match="not both"):
        create_app(entry_services=NS(),services_context=lambda:None)


@pytest.mark.parametrize("csv_enabled", [False, True])
def test_resource_ownership_and_cleanup_on_partial_startup(monkeypatch, csv_enabled):
    import openai
    from risk_platform.knowledge import milvus
    events=[]
    class Engine:
        def connect(self):
            @asynccontextmanager
            async def connection():
                yield NS(scalar=scalar)
            return connection()
        async def dispose(self): events.append("engine-close")
    async def scalar(*args,**kwargs): return "table"
    class Runner:
        def __init__(self,*args,**kwargs): pass
        async def aclose(self): events.append("runner-close")
    @asynccontextmanager
    async def saver(*args,**kwargs):
        assert kwargs=={"setup":False}
        events.append("saver-open")
        try: yield object()
        finally: events.append("saver-close")
    async def close_embedding(): events.append("embedding-close")
    def fake_milvus(uri):
        assert uri=="http://localhost:19530"
        return NS(close=lambda:events.append("milvus-close"))
    monkeypatch.setenv("SILICONFLOW_API_KEY","embedding-key")
    monkeypatch.setenv("MILVUS_URI","http://localhost:19530")
    monkeypatch.setattr(bootstrap,"get_database_url",lambda:"postgresql+psycopg://unused")
    async def close_tool_model(): events.append("tool-model-close")
    monkeypatch.setattr(bootstrap,"create_llm_agents",lambda:(NS(),
        NS(client=NS(close=lambda:events.append("llm-close")),tool_model=NS(root_async_client=NS(close=close_tool_model)))))
    monkeypatch.setattr(bootstrap,"create_async_engine",lambda *a,**kw:Engine())
    monkeypatch.setattr(bootstrap,"async_sessionmaker",lambda *a,**kw:object())
    monkeypatch.setattr(bootstrap,"postgres_checkpointer",saver)
    monkeypatch.setattr(bootstrap,"InvestigationRunner",Runner)
    monkeypatch.setattr(openai,"AsyncOpenAI",lambda **kw:NS(close=close_embedding))
    monkeypatch.setattr(milvus,"get_client",fake_milvus)
    config = settings()
    if csv_enabled:
        from risk_platform.execution.csv_statistics import CsvDataset
        config = config.model_copy(update={"csv_datasets": (CsvDataset(
            dataset_id="train", model_id="M", path="unused.csv", sha256="a" * 64,
            timezone_offset="+08:00", window_start="2025-09-01T00:00:00+08:00",
            window_end="2026-01-01T00:00:00+08:00"),)})
    async def scenario():
        async with bootstrap.local_services(config) as services:
            from risk_platform.workflow.contracts import InitialPlanningInput,UserQuery
            source=InitialPlanningInput(user_query=UserQuery(query="test",model_id="M"),available_capabilities=["KNOWLEDGE_RETRIEVAL"])
            bound = services.tools_for("local-user",source)
            tool = bound[0]
            assert [t.name for t in bound] == (["knowledge_search", "csv_sample_statistics"]
                                              if csv_enabled else ["knowledge_search"])
            planned = services._planning_source("local-user", user_query=source.user_query)
            assert ("COMPUTATION" in planned.available_capabilities) == csv_enabled
            assert tool.execution.scope.model_id=="M" and tool.embedding_client is not None
            assert tool.milvus_client is not None and tool.session_factory is services.sessions
            with pytest.raises(PermissionError): services.tools_for("other",source)
    asyncio.run(scenario())
    assert events==["saver-open","milvus-close","embedding-close","runner-close","saver-close","engine-close","tool-model-close","llm-close"]
    events.clear()
    def fail(uri): raise ConnectionError("private dependency error")
    monkeypatch.setattr(milvus,"get_client",fail)
    with pytest.raises(ConnectionError): asyncio.run(scenario())
    assert events==["saver-open","embedding-close","runner-close","saver-close","engine-close","tool-model-close","llm-close"]


def test_local_launcher_owns_loop_and_binds_loopback(monkeypatch):
    import runpy
    import uvicorn
    from pathlib import Path
    called=[]
    class Server:
        def __init__(self,config): self.config=config
        async def serve(self):
            assert isinstance(asyncio.get_running_loop(),asyncio.SelectorEventLoop)
            called.append((self.config.host,self.config.port,self.config.factory,self.config.app))
    monkeypatch.setattr(uvicorn,"Server",Server)
    main=runpy.run_path(str(Path(__file__).resolve().parents[2]/"scripts"/"serve_local.py"))["main"]
    main()
    assert called==[("127.0.0.1",8000,True,"risk_platform.bootstrap:create_local_app")]


def test_http_smoke_only_sends_question_and_reads_saved_turn(monkeypatch,capsys):
    import runpy
    import httpx
    from pathlib import Path
    called=[]
    view={"status":"ANSWERED","turn_id":"T","result":{"status":"SUCCEEDED"}}
    class Client:
        def __init__(self,**kwargs): assert kwargs["base_url"]=="http://127.0.0.1:8000"
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def post(self,path,*,json):
            called.append(path)
            assert "action" not in json
            return NS(status_code=200,json=lambda:view)
        def get(self,path):
            called.append(path)
            return NS(status_code=200,json=lambda:view)
    monkeypatch.setenv("RISK_LOCAL_API_TOKEN","private-local-token")
    monkeypatch.setattr(httpx,"Client",Client)
    main=runpy.run_path(str(Path(__file__).resolve().parents[2]/"scripts"/"smoke_chat_http.py"))["main"]
    assert main()==0 and called==["/chat/messages","/chat/turns/T"]
    assert "private-local-token" not in capsys.readouterr().out
