"""PostgreSQL checkpointer：显式初始化，仅接受 JSON，不反序列化任意 Python 对象。"""

import json
from contextlib import asynccontextmanager

from sqlalchemy import make_url


class JsonCheckpointSerializer:
    def dumps(self, value):
        def error_only(obj):
            if isinstance(obj, BaseException):
                return {"workflow_error_type": type(obj).__name__}
            raise TypeError("checkpoint 只允许 JSON 数据")
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=error_only).encode("utf-8")

    def loads(self, value):
        def invalid_constant(_):
            raise ValueError("checkpoint 不允许非有限数值")
        return json.loads(value, parse_constant=invalid_constant)

    def dumps_typed(self, value):
        return "json", self.dumps(value)

    def loads_typed(self, value):
        kind, data = value
        if kind != "json":
            raise ValueError("拒绝非 JSON checkpoint")
        return self.loads(data)


@asynccontextmanager
async def postgres_checkpointer(database_url, *, setup=False):
    # 数据库初始化由部署者显式启用；不把建表藏进普通业务调用。
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError:
        raise RuntimeError("PostgreSQL 恢复需要安装 backend 的 recovery 可选依赖") from None
    url = make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    async with AsyncPostgresSaver.from_conn_string(url, serde=JsonCheckpointSerializer()) as saver:
        if setup:
            await saver.setup()
        yield saver
