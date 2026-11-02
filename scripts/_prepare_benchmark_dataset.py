"""Regenerate the versioned synthetic gold fixture; no database writes."""
import argparse
import json
from pathlib import Path
from risk_platform.knowledge.chunking import KnowledgeDocument, DocumentPage
from risk_platform.knowledge.contracts import KnowledgeDocumentType
from risk_platform.knowledge.benchmark import BenchmarkDataset

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--with-reference-answers", action="store_true", help="Generate v2 for answer/judge evaluation")
args = parser.parse_args()
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
reference_answers = {
    "incident-root-cause": "该历史报告中的指标下降源于标签成熟度不足和评估样本选择逻辑：未成熟样本被当作非坏样本，低估坏样本数量及模型效果。仅使用成熟样本复算后，KS 从 0.29 恢复到 0.41，AUC 从 0.70 恢复到 0.77，不足以认定模型真实退化。",
    "incident-maturity": "该报告中 6,440 条样本尚未成熟，占 35.0%。它们被临时当作未发生坏事件计入结果，导致坏样本数量被低估，影响效果指标。",
    "incident-action": "该报告调查初期未确认模型真实退化，暂未触发下线或回滚。应在监控前增加标签成熟度检查，并展示成熟样本数和成熟率；不能直接依据历史案例替当前案件作出回滚决定。",
    "incident-validation": "仅使用成熟样本重新计算，KS 从原监控值 0.29 恢复至 0.41，参考值为 0.43；AUC 从 0.70 恢复至 0.77，参考值为 0.78。",
    "development-label": "坏样本定义为申请后 30 天内出现 DPD >= 7。只有完成完整 30 天表现窗口的样本才进入模型训练和验证。",
    "development-selection": "综合比较效果、稳定性、训练效率和解释能力后选择 LightGBM；经过缺失率、稳定性、相关性和模型贡献度筛选，最终保留 38 个特征。",
}
dataset = BenchmarkDataset.model_validate(dict(
    dataset_id="riskmind-synthetic-reports-v2" if args.with_reference_answers else "riskmind-synthetic-reports-v1",
    annotation_notes="Hand-selected verbatim relevant passages from two synthetic fixtures. Preliminary incomplete relevance judgments requiring domain review. Whole-quote availability is neither Evidence Recall nor answer correctness; no production generalization.",
    documents=documents,
    questions=[dict(question_id=qid, request=dict(query=query, top_k=5),
                    reference_answer=reference_answers[qid] if args.with_reference_answers else None, relevant_quotes=[
        dict(quote_id=f"{qid}-{index}", document_id=documents[di].document_id, document_version="v1", text=text)
        for index, text in enumerate(quotes, 1)]) for qid, query, di, quotes in cases],
))
target = root / ("backend/tests/fixtures/knowledge_benchmark_v2.json" if args.with_reference_answers
                 else "backend/tests/fixtures/knowledge_benchmark_v1.json")
target.parent.mkdir(parents=True, exist_ok=True)
data = dataset.model_dump(mode="json")
if not args.with_reference_answers:
    for question in data["questions"]:
        question.pop("reference_answer")
target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Validated {len(dataset.questions)} questions: {target}")
