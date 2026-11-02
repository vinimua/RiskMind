"""Read-only Stage 6E benchmark against explicitly activated, dedicated routes."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

from risk_platform.knowledge.benchmark import BenchmarkDataset, dataset_digest, run_benchmark
from risk_platform.knowledge.embeddings import EMBEDDING_DIMENSION, EMBEDDING_MODEL, get_embedding_client
from risk_platform.knowledge.generation_evaluation import LLMGenerationEvaluator


async def execute(args):
    dataset = BenchmarkDataset.model_validate_json(args.dataset.read_text(encoding="utf-8"))
    report = {"status": "INCONCLUSIVE", "dataset_id": dataset.dataset_id,
              "dataset_digest": dataset_digest(dataset), "started_at": datetime.now(timezone.utc).isoformat(),
              "embedding_model": EMBEDDING_MODEL, "embedding_dimension": EMBEDDING_DIMENSION,
              "python": platform.python_version(), "tokenizer_sha256": None,
              "scope": "quote-labeled retrieval; optional LLM-judged generation; no business evidence qualification"}
    generation_client = None
    try:
        report["stage"] = "tokenizer_configuration"
        counter = None
        if args.tokenizer_json:
            from tokenizers import Tokenizer
            tokenizer = Tokenizer.from_file(str(args.tokenizer_json))
            report["tokenizer_sha256"] = hashlib.sha256(args.tokenizer_json.read_bytes()).hexdigest()
            counter = lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids)
        report["stage"] = "embedding_configuration"
        embedder = get_embedding_client()
        evaluator = None
        if getattr(args, "evaluate_generation", False):
            from openai import AsyncOpenAI
            from risk_platform.knowledge.embeddings import DEFAULT_SILICONFLOW_BASE_URL
            if not args.answer_model or not args.judge_model:
                raise ValueError("生成侧评测必须提供 --answer-model 和 --judge-model")
            api_key = os.environ.get("RAG_EVAL_API_KEY") or os.environ.get("SILICONFLOW_API_KEY")
            if not api_key:
                raise ValueError("未配置评测模型 API Key")
            generation_client = AsyncOpenAI(
                api_key=api_key, max_retries=0, timeout=60,
                base_url=os.environ.get("RAG_EVAL_BASE_URL") or os.environ.get(
                    "SILICONFLOW_BASE_URL", DEFAULT_SILICONFLOW_BASE_URL),
            )
            evaluator = LLMGenerationEvaluator(client=generation_client,
                                               answer_model=args.answer_model, judge_model=args.judge_model)
        report["stage"] = "pipeline"
        result = await run_benchmark(dataset, repeats=args.repeats, count_tokens=counter, embedding_client=embedder,
                                     generation_evaluator=evaluator)
        report.update(result)
        summary = {}
        for profile in ("baseline-v1", "parent-child-v1"):
            rows = [row for row in result["rows"] if row["profile"] == profile]
            metrics = {}
            for key in rows[0]["metrics"]:
                values = [row["metrics"][key] for row in rows]
                if all(isinstance(value, (int, float)) for value in values):
                    metrics[key] = mean(values)
                elif any(value is None for value in values) and all(
                    value is None or isinstance(value, (int, float)) for value in values
                ):
                    metrics[key] = None  # 分母未定义时不静默缩小样本集合。
            latencies = sorted(row["total_ms"] for row in rows)
            summary[profile] = {"macro_metrics": metrics, "latency_p50_ms": median(latencies),
                                "latency_p95_ms": latencies[max(0, math.ceil(len(latencies) * .95) - 1)]}
            if evaluator is not None:
                generations = [row["generation"] for row in rows]
                complete = all(item["status"] == "SUCCEEDED" for item in generations)
                summary[profile]["generation_status"] = "SUCCEEDED" if complete else "INCONCLUSIVE"
                summary[profile]["generation_success_count"] = sum(item["status"] == "SUCCEEDED" for item in generations)
                # 不用成功子集的均值冒充完整实验结果。
                summary[profile]["generation_macro_metrics"] = {
                    key: mean(item["metrics"][key] for item in generations)
                    for key in generations[0].get("metrics", {})
                    if complete and all(isinstance(item["metrics"][key], (int, float)) for item in generations)
                }
                if complete:
                    summary[profile]["generation_mean_ms"] = mean(item["generation_ms"] for item in generations)
                    summary[profile]["judge_mean_ms"] = mean(item["judge_ms"] for item in generations)
                for key in ("prompt_tokens", "completion_tokens", "judge_prompt_tokens", "judge_completion_tokens"):
                    values = [item.get(key) for item in generations]
                    summary[profile][f"{key}_total"] = sum(values) if complete and all(type(v) is int for v in values) else None
        report["summary"] = summary
        report["stage"] = "complete"
    except Exception as error:
        # Do not serialize dependency exception messages: they may contain DSNs,
        # authorization headers or provider response bodies. Failure is explicit.
        report["status"] = "INCONCLUSIVE"
        report.pop("rows", None)
        report.pop("summary", None)
        report["error_type"] = type(error).__name__
        report["error"] = "Benchmark incomplete; check credentials, dependencies, corpus and serving routes."
    finally:
        if generation_client is not None:
            await generation_client.close()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{report['status']}: {args.output}")
    return 0 if report["status"] == "SUCCEEDED" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--tokenizer-json", type=Path, help="Pinned downstream model tokenizer; optional tokenizers package")
    parser.add_argument("--evaluate-generation", action="store_true", help="Generate answers and run structured LLM review")
    parser.add_argument("--answer-model", help="Explicit OpenAI-compatible answer model ID")
    parser.add_argument("--judge-model", help="Explicit OpenAI-compatible review model ID")
    args = parser.parse_args()
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(execute(args)))


if __name__ == "__main__":
    main()
