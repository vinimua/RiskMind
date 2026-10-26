"""Regenerate the versioned synthetic gold fixture; no database writes."""
from pathlib import Path
from risk_platform.knowledge.chunking import KnowledgeDocument, DocumentPage
from risk_platform.knowledge.contracts import KnowledgeDocumentType
from risk_platform.knowledge.benchmark import BenchmarkDataset

root = Path(__file__).resolve().parents[1]
documents = []
for name, identity, kind in (
    ("credit_v3_model_development_report_fixture.md", "MODEL-DEV-credit_v3", KnowledgeDocumentType.MODEL_DEVELOPMENT_REPORT),
    ("credit_v3_incident_report_fixture.md", "CASE-2026-018", KnowledgeDocumentType.INCIDENT_REPORT),
):
    content = (root / "docs/runbooks" / name).read_text(encoding="utf-8")
    documents.append(KnowledgeDocument(
        document_id=identity, document_version="v1", document_type=kind,
        title=content.splitlines()[0].removeprefix("# "), model_id="credit_v3",
        source_uri="knowledge://fixtures/" + identity, pages=(DocumentPage(content=content),),
    ))

cases = [
    ("incident-root-cause", "credit_v3 的 KS、AUC 为什么下降？根因机制和复算证据是什么？", 1, [
        "标签成熟度不足导致模型效果指标被低估",
        "未成熟样本被临时按“未发生坏事件”计入结果，导致坏样本数量被低估。",
        "| KS | 0.29 | 0.41 | 0.43 |", "| AUC | 0.70 | 0.77 | 0.78 |"]),
    ("incident-maturity", "CASE-2026-018 有多少标签尚未成熟，影响是什么？", 1, [
        "| 未成熟 | 6,440 | 35.0% |",
        "未成熟样本被临时按“未发生坏事件”计入结果，导致坏样本数量被低估。"]),
    ("incident-action", "这次异常是否应该回滚模型，后续应采取哪些监控措施？", 1, [
        "调查初期尚未确认模型本身发生真实退化，因此暂未触发模型下线或回滚。",
        "在模型监控前增加标签成熟度检查", "在监控结果中展示成熟样本数和成熟率"]),
    ("incident-validation", "仅使用成熟标签样本验证后 KS 和 AUC 恢复到多少？", 1, [
        "| KS | 0.29 | 0.41 | 0.43 |", "| AUC | 0.70 | 0.77 | 0.78 |"]),
    ("development-label", "credit_v3 的坏样本如何定义？哪些样本可以进入训练和验证？", 0, [
        "坏样本：申请后 30 天内出现 `DPD >= 7`", "只有完成完整 30 天表现窗口的样本才进入模型训练与验证。"]),
    ("development-selection", "credit_v3 最终选择了什么模型，筛选后保留多少特征？", 0, [
        "综合比较模型效果、稳定性、训练效率和解释能力后，最终选择 LightGBM 作为 `credit_v3` 的主模型。",
        "经过缺失率、稳定性、相关性和模型贡献度筛选后，最终保留 38 个特征。"]),
]
dataset = BenchmarkDataset.model_validate(dict(
    dataset_id="riskmind-synthetic-reports-v1",
    annotation_notes="Hand-selected verbatim relevant passages from two synthetic fixtures. Preliminary incomplete relevance judgments requiring domain review. Whole-quote availability is neither Evidence Recall nor answer correctness; no production generalization.",
    documents=documents,
    questions=[dict(question_id=qid, request=dict(query=query, top_k=5), relevant_quotes=[
        dict(quote_id=f"{qid}-{index}", document_id=documents[di].document_id, document_version="v1", text=text)
        for index, text in enumerate(quotes, 1)]) for qid, query, di, quotes in cases],
))
target = root / "backend/tests/fixtures/knowledge_benchmark_v1.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(dataset.model_dump_json(indent=2) + "\n", encoding="utf-8")
print(f"Validated {len(dataset.questions)} questions: {target}")
