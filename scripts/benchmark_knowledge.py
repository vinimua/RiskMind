"""Read-only Stage 6E benchmark against explicitly activated, dedicated routes."""

import argparse
import asyncio
import hashlib
import json
import math
import platform
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

from risk_platform.knowledge.benchmark import BenchmarkDataset, dataset_digest, run_benchmark
from risk_platform.knowledge.embeddings import EMBEDDING_DIMENSION, EMBEDDING_MODEL, get_embedding_client


async def execute(args):
    dataset = BenchmarkDataset.model_validate_json(args.dataset.read_text(encoding="utf-8"))
    report = {"status": "INCONCLUSIVE", "dataset_id": dataset.dataset_id,
              "dataset_digest": dataset_digest(dataset), "started_at": datetime.now(timezone.utc).isoformat(),
              "embedding_model": EMBEDDING_MODEL, "embedding_dimension": EMBEDDING_DIMENSION,
              "python": platform.python_version(), "tokenizer_sha256": None,
              "scope": "synthetic exact-quote context availability; no answer/evidence qualification"}
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
        report["stage"] = "pipeline"
        result = await run_benchmark(dataset, repeats=args.repeats, count_tokens=counter, embedding_client=embedder)
        report.update(result)
        summary = {}
        for profile in ("baseline-v1", "parent-child-v1"):
            rows = [row for row in result["rows"] if row["profile"] == profile]
            metrics = {}
            for key in rows[0]["metrics"]:
                values = [row["metrics"][key] for row in rows]
                if all(isinstance(value, (int, float)) for value in values):
                    metrics[key] = mean(values)
            latencies = sorted(row["total_ms"] for row in rows)
            summary[profile] = {"macro_metrics": metrics, "latency_p50_ms": median(latencies),
                                "latency_p95_ms": latencies[max(0, math.ceil(len(latencies) * .95) - 1)]}
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
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{report['status']}: {args.output}")
    return 0 if report["status"] == "SUCCEEDED" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--tokenizer-json", type=Path, help="Pinned downstream model tokenizer; optional tokenizers package")
    args = parser.parse_args()
    if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(execute(args)))


if __name__ == "__main__":
    main()
