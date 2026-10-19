# 数据库迁移

Stage 5A 初始化正式 Alembic 配置，并创建 `knowledge_chunks`。

```powershell
$env:RISK_DATABASE_URL="postgresql+psycopg://risk_dev:...@localhost:5432/risk_platform"
alembic -c alembic.ini upgrade head
alembic -c alembic.ini downgrade base
```

迁移必须支持升级验证与回滚演练；应用运行时不调用 `create_all()`。
