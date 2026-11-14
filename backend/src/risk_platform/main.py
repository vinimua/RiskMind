"""API composition root; business dependencies and authentication are explicitly injected."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from fastapi import FastAPI
from risk_platform.core.observability import shutdown_observability
from risk_platform.api.routes.health import router as health_router

# main.create_app() 创建 HTTP 应用
#   → 定义生命周期函数
#   → 创建 FastAPI(lifespan=lifespan)
#   → 注册路由
#   → 返回 app
def create_app(*, entry_services=None, authenticate=None, services_context=None) -> FastAPI:
    """Accept either caller-owned services or a factory for application-owned services."""
    if entry_services is not None and services_context is not None:
        raise ValueError("Provide entry_services or services_context, not both")

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with AsyncExitStack() as stack:
                if services_context is not None:
                    # 工厂在启动时才执行；上下文负责初始化、失败清理和退出释放。
                    app.state.entry_services = await stack.enter_async_context(services_context())
# 这时才真正进入 local_services(settings)，依次创建：
# 聊天与调查 Agent
#   → 数据库连接池、Session 工厂
#   → 检查关键表
#   → PostgreSQL Checkpointer
#   → InvestigationRunner
#   → Embedding 客户端
#   → Milvus 客户端
#   → 组装 EntryServices
# local_services() 最后执行：
# yield EntryServices(...)
# 把准备好的服务交回 main。main 再将它们挂到应用上：
                    app.state.authenticate = authenticate
                # 逆序退出：先关闭可观测性，再释放服务，保持原有关闭顺序。
                # 服务初始化失败时不会登记此回调，与原本的生命周期行为一致。
                stack.push_async_callback(asyncio.to_thread, shutdown_observability)
                yield
        finally:
            if services_context is not None:
                # 只撤下本生命周期持有的服务及认证引用。
                app.state.entry_services = None
                app.state.authenticate = None
#     AsyncExitStack 的规则是：后登记的清理动作先执行。
# main 先进入服务上下文，再登记可观测性关闭，所以退出顺序是：
# 关闭可观测性
#   → 退出 local_services
#       → 关闭 Milvus
#       → 关闭 Embedding 客户端
#       → 关闭 Runner
#       → 退出 Checkpointer
#       → 释放数据库连接池
#       → 关闭共享 LLM 客户端
#   → 清空 app.state 中的服务和认证引用

# app.state.authenticate：认证请求，取得用户身份。
# app.state.entry_services：取得聊天等业务服务。

    app = FastAPI(title="Credit Risk Platform", version="0.1.0", lifespan=lifespan)
    app.state.entry_services = entry_services
    app.state.authenticate = authenticate if services_context is None else None
    from risk_platform.api.routes.entrypoints import router as entry_router
    app.include_router(entry_router)
    app.include_router(health_router)
    return app


app = create_app()


# POST /chat/messages
#   → principal 调用 app.state.authenticate
#   → 校验 Token，取得 actor
#   → 路由取得 app.state.entry_services
#   → EntryServices.chat(...)
#   → 执行聊天入口业务逻辑
# actor 是这个函数验证成功后返回的用户 ID。