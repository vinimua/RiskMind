"""Small CLI for staging Markdown and recovering the Milvus read model."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

from risk_platform.db.database import dispose_engine, get_session_factory
from risk_platform.knowledge.chunking import (
    DocumentPage,
    KnowledgeDocument,
    chunk_document,
)
from risk_platform.knowledge.contracts import KnowledgeDocumentType
from risk_platform.knowledge.ingestion import (
    DEFAULT_BATCH_SIZE,
    ingest_pending,
    rebuild_collection,
)
from risk_platform.knowledge.repository import stage_chunks


_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _check_document_heading(markdown: str, title: str) -> None:
    """Require the first semantic Markdown heading to match the document title."""

    fence = None
    for line in _HTML_COMMENT_RE.sub("", markdown).splitlines():
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker[0]
            elif marker[0] == fence:
                fence = None
            continue
        if fence is not None:
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            if len(heading.group(1)) != 1 or heading.group(2).strip() != title:
                raise ValueError("first Markdown heading must be H1 and match --title")
            return
    raise ValueError("Markdown must contain a first H1 matching --title")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RiskMind knowledge ingestion")
    commands = parser.add_subparsers(dest="command", required=True)

    stage = commands.add_parser("stage", help="stage one canonical Markdown document")
    stage.add_argument("path", type=Path)
    stage.add_argument("--document-id", required=True)
    stage.add_argument("--document-version", required=True)
    stage.add_argument(
        "--document-type",
        type=KnowledgeDocumentType,
        choices=list(KnowledgeDocumentType),
        required=True,
    )
    stage.add_argument("--title", required=True)
    stage.add_argument("--source-uri", required=True)
    stage.add_argument("--model-id")

    for name in ("resume", "rebuild"):
        command = commands.add_parser(name)
        command.add_argument("--collection", required=True)
        command.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    return parser


async def _run(args: argparse.Namespace) -> dict[str, int]:
    if args.command == "stage":
        markdown = args.path.read_text(encoding="utf-8-sig")
        _check_document_heading(markdown, args.title)
        document = KnowledgeDocument(
            document_id=args.document_id,
            document_version=args.document_version,
            document_type=args.document_type,
            title=args.title,
            source_uri=args.source_uri,
            model_id=args.model_id,
            pages=(DocumentPage(content=markdown),),
        )
        chunks = chunk_document(document)
        if not chunks:
            raise ValueError("document produced no knowledge chunks")
        sessions = get_session_factory()
        async with sessions() as session:
            async with session.begin():
                return {"staged": await stage_chunks(session, chunks)}
    if args.command == "resume":
        return {
            "ready": await ingest_pending(
                collection=args.collection, batch_size=args.batch_size
            )
        }
    return {
        "upserted": await rebuild_collection(
            collection=args.collection, batch_size=args.batch_size
        )
    }


async def _run_and_close(args: argparse.Namespace) -> dict[str, int]:
    try:
        return await _run(args)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        result = asyncio.run(_run_and_close(args))
    except Exception as exc:
        print(f"knowledge {args.command} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
