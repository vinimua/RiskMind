"""Explicit single-user local composition; not a production identity provider.

Run: uvicorn risk_platform.bootstrap:create_local_app --factory --host 127.0.0.1
No schema setup or external model requests happen merely by importing this module.

阅读顺序：create_local_app → main.create_app 中的 lifespan → local_services → EntryServices。
本文件是本地开发的“组合入口”：负责把已有组件接起来，不实现调查业务或第二套流程。
配置与权限来自环境和确定性校验；客户端由应用生命周期持有，不进入可持久化业务 State。
"""
import asyncio
import hmac
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from risk_platform.execution.csv_statistics import CsvDataset, CsvStatisticsTool

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from risk_platform.core.llm import create_llm_agents
from risk_platform.db.database import get_database_url
from risk_platform.entrypoints import EntryServices
from risk_platform.execution.contracts import KnowledgeExecutionContext
from risk_platform.knowledge.indexing.profiles import IndexProfile
from risk_platform.memory.contracts import Identity
from risk_platform.workflow.checkpoints import postgres_checkpointer
from risk_platform.workflow.knowledge_tool import KnowledgeSearchTool
from risk_platform.workflow.runner import InvestigationRunner

# main 统一安排：初始化本地依赖 → 处理请求 → 关闭可观测性 → 释放本地依赖。

def required(name):
    # 必填配置缺失时立即报错，避免带着空凭证或隐式默认值继续启动。
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


class LocalSettings(BaseModel):
    """Local identity and explicit grants, independent of any model output."""
# LocalSettings 同时提供本地认证与授权规则。
# - authenticate()：检查 Bearer Token，回答“请求者是谁”。
# - authorize_conversation()：检查是否允许聊天操作。
# - authorize_resource()：检查是否允许操作指定业务模型的调查。
# - authorize_knowledge()：检查知识检索的身份、范围和上限。
# local_services() 将依赖组装为 EntryServices，由 main 的生命周期交给接口层。
    # 这里只保存单用户身份、授权范围和本地执行限制，不是全项目配置中心。
    # frozen 禁止字段重新赋值；extra 拒绝未知字段；错误输出隐藏输入值。
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    actor_id: Identity
    # SecretStr 和 repr=False 减少凭证被意外打印的机会；不代表凭证已加密保存。
    api_token: SecretStr = Field(min_length=32, repr=False)
    # 这是 credit-v3 等受监测业务模型的 ID，不是 DeepSeek 等大模型名称。
    allowed_model_ids: tuple[Identity, ...] = Field(min_length=1)
    knowledge_profile: IndexProfile
    timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    max_top_k: int = Field(default=5, gt=0, strict=True)
    csv_datasets: tuple[CsvDataset, ...] = ()

    @model_validator(mode="after")
    def unique_models(self):
        if len(set(self.allowed_model_ids)) != len(self.allowed_model_ids):
            raise ValueError("allowed_model_ids contains duplicates")
        if len({d.dataset_id for d in self.csv_datasets}) != len(self.csv_datasets):
            raise ValueError("CSV dataset IDs must be unique")
        if any(d.model_id not in self.allowed_model_ids for d in self.csv_datasets):
            raise ValueError("CSV dataset model outside local grant")
        return self

    @classmethod
    def from_environment(cls):
        # 必须主动开启本地模式，避免部署时无意采用单用户身份。
        # 这里读取进程环境，不自动加载 .env；超时和 top_k 使用上面的默认值。
        if os.environ.get("RISK_LOCAL_MODE") != "1":
            raise RuntimeError("Set RISK_LOCAL_MODE=1 to explicitly enable single-user local authentication")
        return cls(actor_id=required("RISK_LOCAL_ACTOR_ID"),
            api_token=required("RISK_LOCAL_API_TOKEN"),
            allowed_model_ids=json.loads(required("RISK_ALLOWED_MODEL_IDS")),
            knowledge_profile=required("RISK_KNOWLEDGE_PROFILE"),
            csv_datasets=json.loads(os.environ.get("RISK_CSV_DATASETS", "[]")))

    def authenticate(self, request):
        # 认证回答“是谁”：校验 Authorization: Bearer <token> 后返回配置中的身份。
        # compare_digest 使用恒定时间比较，降低普通字符串比较带来的时序泄露风险。
        value = request.headers.get("authorization", "")
        scheme, separator, token = value.partition(" ")
        if (not separator or scheme.lower() != "bearer"
                or not hmac.compare_digest(token.encode(), self.api_token.get_secret_value().encode())):
            raise PermissionError("Invalid local bearer credential")
        return self.actor_id

    def authorize_conversation(self, actor, action, resource):
        # 授权回答“能做什么”：本地单用户仅开放这些聊天动作；此处不使用 resource。
        return actor == self.actor_id and action in {"chat:create", "chat:read", "chat:send"}

    def authorize_resource(self, actor, action, resource):
        # 调查操作还必须落在允许的业务模型范围内，不能只凭用户身份放行。
        return (actor == self.actor_id and resource.model_id in self.allowed_model_ids
                and action in {"investigation:create", "investigation:read", "investigation:continue"})

    def authorize_knowledge(self, actor, action, execution):
        # 工具调用另行核对执行身份、模型范围、索引配置及检索数量上限。
        # 这些限制由服务端配置决定，不能由 Agent 输出扩大。
        return (actor == self.actor_id == execution.actor_id and action == "knowledge:search"
                and execution.scope.model_id in self.allowed_model_ids
                and execution.index_profile == self.knowledge_profile
                and execution.max_top_k <= self.max_top_k)


@asynccontextmanager
async def local_services(settings):
    """Own every client/pool; unwind partially completed startup on failure."""
    from openai import AsyncOpenAI
    from risk_platform.knowledge.embeddings import EmbeddingClient, EMBEDDING_MODEL, DEFAULT_SILICONFLOW_BASE_URL
    from risk_platform.knowledge.milvus import get_client

    database_url = get_database_url()
    embedding_key = required("SILICONFLOW_API_KEY")
    milvus_uri = required("MILVUS_URI")
    # 每取得一个资源就登记清理动作，退出时逆序释放。
    # 启动到一半发生异常，也会清理此前已登记的资源。
    async with AsyncExitStack() as stack:
        chat, investigator = create_llm_agents()
        # 两个 Agent 共享模型客户端；同步 close 放在线程中执行，避免阻塞事件循环。
        stack.push_async_callback(asyncio.to_thread, investigator.client.close)
        stack.push_async_callback(investigator.tool_model.root_async_client.close)
        engine = create_async_engine(database_url, pool_pre_ping=True)
        stack.push_async_callback(engine.dispose)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        # sessions 是 Session 工厂，不是所有请求共用的一个事务。
        # Fail before accepting requests if deployment migrations/checkpoints are absent.
        # 这里只检查关键表是否存在，并非完整 schema 校验；建表与迁移需部署步骤显式完成。
        async with engine.connect() as connection:
            for table in ("chat_entry_decisions", "investigation_runs", "checkpoints"):
                if await connection.scalar(text("SELECT to_regclass(:name)"), {"name": table}) is None:
                    raise RuntimeError("Database schema is not prepared; apply migrations and checkpoint setup explicitly")
        saver = await stack.enter_async_context(postgres_checkpointer(database_url, setup=False))
        # 复用原调查 Runner 和持久化检查点机制；本入口不另建恢复流程。
        runner = InvestigationRunner(investigator, session_factory=sessions, checkpointer=saver)
        stack.push_async_callback(runner.aclose)
        embedding_api = AsyncOpenAI(api_key=embedding_key,
            base_url=os.environ.get("SILICONFLOW_BASE_URL") or DEFAULT_SILICONFLOW_BASE_URL,
            timeout=settings.timeout_seconds, max_retries=0)
        stack.push_async_callback(embedding_api.close)
        embedder = EmbeddingClient(client=embedding_api, model=EMBEDDING_MODEL)
        # Embedding 用于检索向量，与聊天/调查使用的大模型客户端分别配置。
        # Explicit URI creates an application-owned client, not the module singleton.
        milvus = await asyncio.to_thread(get_client, milvus_uri)
        stack.push_async_callback(asyncio.to_thread, milvus.close)

        def tools_for(actor, source):
            # 为本次调查绑定可信的检索范围。闭包复用应用资源，不把客户端塞入 State。
            model_id = source.monitoring_alert.model_id if source.monitoring_alert else source.user_query.model_id
            if actor != settings.actor_id or model_id not in settings.allowed_model_ids:
                raise PermissionError("Model outside local grant")
            execution = KnowledgeExecutionContext(actor_id=actor,
                index_profile=settings.knowledge_profile, scope={"model_id": model_id},
                max_top_k=settings.max_top_k, timeout_seconds=settings.timeout_seconds)
            bound = (KnowledgeSearchTool(execution, settings.authorize_knowledge,
                session_factory=sessions, embedding_client=embedder, milvus_client=milvus),)
            datasets = tuple(d for d in settings.csv_datasets if d.model_id == model_id)
            if datasets:
                bound += (CsvStatisticsTool(actor_id=actor, model_id=model_id, datasets=datasets),)
            return bound

        # yield 前完成资源准备，yield 期间由 HTTP 入口使用这些服务；退出后统一清理。
        yield EntryServices(sessions=sessions, runner=runner, chat_agent=chat,
            tools_for=tools_for, authorize_resource=settings.authorize_resource,
            authorize_conversation=settings.authorize_conversation,
            timeout_seconds=settings.timeout_seconds)


def create_local_app():
    """Opt-in factory. Resources are initialized by main's lifespan at startup."""
    from risk_platform.main import create_app

    settings = LocalSettings.from_environment()
    # partial 只绑定配置，不立即初始化资源；main 在生命周期启动时调用它。
    # 创建应用时显式注入，不再事后替换 Router 的生命周期处理器。
    return create_app(
        services_context=partial(local_services, settings),
        authenticate=settings.authenticate,
    )
