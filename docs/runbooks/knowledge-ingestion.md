# Knowledge ingestion CLI

Install the backend with its database and Milvus dependencies, then apply the
Alembic migration before using the CLI:

```powershell
python -m pip install -e "./backend[database,knowledge]"
cd backend
alembic -c alembic.ini upgrade head
cd ..
```

Set `RISK_DATABASE_URL`,
`SILICONFLOW_API_KEY`, and, if needed, `MILVUS_URI` in the environment. Keep API
keys out of source files.

Run these commands from the project root:

```powershell
python -m risk_platform.knowledge.cli stage docs/runbooks/credit_v3_model_development_report_fixture.md `
  --document-id MODEL-DEV-credit_v3 --document-version v1 `
  --document-type MODEL_DEVELOPMENT_REPORT `
  --title "credit_v3 模型开发报告（Synthetic Fixture）" `
  --source-uri knowledge://models/credit_v3/v1 `
  --model-id credit_v3

python -m risk_platform.knowledge.cli resume --collection riskmind_knowledge_v003
python -m risk_platform.knowledge.cli rebuild --collection riskmind_knowledge_v004
```

`stage` accepts canonical Markdown with a first H1 equal to `--title`. It stages
one immutable document version; repeating the same input is a no-op. This CLI
does not parse PDFs or assign PDF page numbers. A PDF parser must supply the
per-page Markdown through the document contract separately.

`resume` handles only `PENDING` chunks. It marks each batch `READY` after Milvus
upsert and flush; rerun it after a failure.

`rebuild` reads both `PENDING` and `READY`, re-embeds them, and upserts them to
the specified physical collection. It never changes PostgreSQL statuses. Use
a **new empty physical collection** for a complete rebuild: upsert does not
remove stale rows already present in an existing collection. An interrupted
rebuild can be rerun against that same new collection. If PostgreSQL gains
chunks while rebuild is running, the command reports an error; rerun it to
include the new chunks. Neither command switches the active Milvus alias.
