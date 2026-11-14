# RiskMind 企业异常模式模拟知识语料（50 份）

**内容声明：全包均为高保真合成示例，非真实企业内部报告。** 公开的监管处罚与指南仅提供研究方向和技术风险模式，任何机构名称、Case ID、模型报告、指标、样本量、调查时间线、负责人和证据标识均为虚构。不要将这些“模拟根因”提交到生产 Policy Gate。

## 语料范围

- `MODEL_DEVELOPMENT_REPORT/`：15 份模型开发报告，涉及 `9` 个模型 ID 和多个版本，使用 RiskMind 当前两类文档契约。
- `INCIDENT_REPORT/`：35 份异常调查报告，每份与模型开发报告的 model_id/model_version 对应。
- `05_metadata/manifest.json`、`manifest.csv`：50 份文档的确定性身份、模型、类型、路径及来源占位 URI。
- `05_metadata/qa_seed_150.jsonl`：150 条自动生成的、有原文精确命中片段的问题草案，**未经人工评测标注**，不能作为正式 Gold 标注直接宣称模型效果。
- `05_metadata/stage_commands.sh`：与当前 `risk_platform.knowledge.cli stage` 契约一致的命令样例；执行前需设置测试数据库及 Python 环境。
- `05_metadata/validation.json`：格式、身份、引用及自动题目校验结果。

## 类别分布

| 类别 | 份数 |
|---|---:|
| 客群与渠道结构 | 6 |
| 数据完整性与血缘 | 6 |
| 评分与特征分布 | 6 |
| 多因竞争及证据不足 | 2 |
| 标签成熟度及定义 | 6 |
| 真实性能恶化 | 4 |
| 模型及特征版本 | 5 |

## 入库使用指南

1. 先审阅 `manifest.csv` 的 model_id、版本和所有案例是否适合你的测试目标；文档源 URI 使用保留的 `riskmind.example.test` **占位地址**，不是可访问的公网上游文件。正式上线须替换为可授权、稳定的真实内部来源 URI，并进行新版本登记。
2. 在隔离环境按 `stage_commands.sh` 逐份登记；`stage` 不等同于向量物化、更不等于激活线上 Serving Route。官方 CLI 还需要独立的 `resume/rebuild/validate/activate` 流程（按项目 runbook）。
3. 评测时将这些资料与其他脱敏真实长文档分成两个测试集，分别报告 synthetic/real 结果，不能混在一起对外声称生产性能。
4. 用 `qa_seed_150.jsonl` 先做专家核查，补充跨文档多跳、同症状不同根因、无答案、时间冲突与模型版本相近等困难问题，并冻结独立测试集。
5. 症状、数值、历史原因都只能当作知识线索。新的 Investigation 必须由当前授权事实工具完成核验；Report 绝不代替当前 Evidence Store。

## 公共参考与合成关系

这些公开材料并**不**证明本包中任意虚构 Case 真实发生，只用于确认行业中确有“模型监控、数据治理、日期属性/评分代码变更、输入质量、回测和审计”这些问题类型。

- [SR26] 美国联邦储备系统《Revised Guidance on Model Risk Management》（2026）——持续监测、效果验证和模型限制：https://www.federalreserve.gov/supervisionreg/srletters/sr2602.htm
- [BCBS239] 巴塞尔银行监管委员会 BCBS 239 ——风险数据准确性、完整性、时效性及数据血缘：https://www.bis.org/committees/bcbs/basel-framework/standard/srp/36/inforce/2019-12-15/published/2019-12-15
- [EBA] 欧洲银行管理局《Guidelines on Loan Origination and Monitoring》——输入质量、输出监控、可追溯性：https://www.eba.europa.eu/activities/single-rulebook/regulatory-activities/credit-risk/guidelines-loan-origination-and-monitoring
- [EQUIFAX] 美国 CFPB 2025 年 Equifax 执法材料——生产评分服务代码变更引发错误日期属性及错误评分的公开失效类型：https://www.consumerfinance.gov/enforcement/actions/equifax-inc-and-equifax-information-services-llc/

其他公开执法事实可参见：2020 OCC 对 Citibank 的数据治理与内控处罚：https://occ.gov/news-issuances/news-releases/2020/nr-occ-2020-132.html；2019 CFPB 对 BSI Financial Services 的贷款转移数据问题：https://www.consumerfinance.gov/enforcement/actions/servis-one-inc-db-bsi-financial-services/。

## 与真实业务数据的边界

文档没有客户 PII、银行卡号、真实贷款账务或模型参数。Case 的证据ID（SIM-*）只用于 RAG 引用测试，不对应真实账本记录。当前包没有真实人工标注的企业事故，未达到“真实长文档生产评测”的最终目标。
