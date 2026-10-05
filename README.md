# 信贷风控模型智能监测与自主迭代平台

这是独立新项目的**开发骨架**，不是已经实现风控闭环的成品。请先阅读 [技术设计](docs/technical-design.md) 和 [Codex 开发约束](AGENTS.md)。

## 目录边界

- `backend/src/risk_platform/workflow/`：唯一业务编排器（LangGraph）；目前仅保留目录。
- `backend/src/risk_platform/agents/`：四类 Agent 的结构化输入输出和提示词；目前仅保留目录。
- `backend/src/risk_platform/modules/`：监测、证据、规则、训练、验证、审批、发布等确定性业务模块。
- `backend/src/risk_platform/integrations/`：LightRAG、模型注册中心、对象存储、线上服务的适配器。
- `backend/src/risk_platform/jobs/`：后台任务执行入口；不另建业务状态机。
- `web/`：Next.js 管理界面骨架。
- `infra/`：本地基础设施示例；生产部署需要单独安全审查。

## 本地启动最小 API

在仓库根目录执行（Python 3.11+）：

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
uvicorn risk_platform.main:app --reload
```

访问 `http://127.0.0.1:8000/health/live`。该接口只代表 API 进程存活，**不代表数据库、知识库或完整业务流程就绪**。

前端需 Node.js 20.9+；在 `web/` 执行 `npm install`、`npm run dev`。首次安装后应提交生成的 lockfile。当前页面仅展示项目骨架，不连接后端业务接口。

可将 `.env.example` 复制为 `.env` 后，用 `docker compose --env-file .env -f infra/compose.yaml up -d` 启动本地 PostgreSQL、Redis、MinIO。示例凭据仅供本机开发，不能用于生产。

## 开发顺序

1. 案件、证据快照、规则版本、审计与数据库迁移。
2. 监测事件和 LangGraph 案件图；先实现确定性路径与断点恢复。
3. LightRAG 适配器、工具注册中心与四类 Agent。
4. 训练、独立验证、规则准入、人工审批。
5. 灰度、观察、回滚及失败注入测试。

任何阈值、标签成熟期、审批角色、发布比例，都应在新项目配置或规则中心明确，不要从演示代码推断为银行生产标准。
