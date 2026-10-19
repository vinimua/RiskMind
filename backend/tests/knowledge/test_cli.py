import asyncio

import pytest

from risk_platform.knowledge import cli


@pytest.mark.parametrize(
    "markdown",
    [
        "## 模型报告\n\n正文",
        "# credit_v2 模型报告\n\n正文",
        "```markdown\n# credit_v3 模型报告\n```\n## 正文\n内容",
    ],
)
def test_stage_rejects_missing_or_mismatched_first_h1(markdown: str) -> None:
    with pytest.raises(ValueError, match="H1"):
        cli._check_document_heading(markdown, "credit_v3 模型报告")


def test_stage_accepts_matching_h1_after_comment_and_code_fence() -> None:
    markdown = (
        "<!-- synthetic fixture -->\n"
        "```markdown\n# 代码里的标题\n```\n"
        "# credit_v3 模型报告\n\n## 模型评估\nKS 与 AUC。"
    )
    cli._check_document_heading(markdown, "credit_v3 模型报告")


def test_stage_cli_chunks_and_stages_one_document_transactionally(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source = tmp_path / "report.md"
    source.write_text("# credit_v3 模型报告\n\n## 模型评估\nKS 与 AUC。", encoding="utf-8")
    staged = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def begin(self):
            return self

    async def fake_stage(_session, chunks):
        staged.extend(chunks)
        return len(chunks)

    monkeypatch.setattr(cli, "get_session_factory", lambda: Session)
    monkeypatch.setattr(cli, "stage_chunks", fake_stage)
    args = cli._parser().parse_args(
        [
            "stage",
            str(source),
            "--document-id", "MODEL-DEV-credit_v3",
            "--document-version", "v1",
            "--document-type", "MODEL_DEVELOPMENT_REPORT",
            "--title", "credit_v3 模型报告",
            "--source-uri", "knowledge://models/credit_v3",
            "--model-id", "credit_v3",
        ]
    )

    result = asyncio.run(cli._run(args))

    assert result == {"staged": 1}
    assert len(staged) == 1
    assert staged[0].document_id == "MODEL-DEV-credit_v3"
    assert staged[0].title == "credit_v3 模型报告"
    assert staged[0].section_path == "模型评估"


def test_resume_and_rebuild_cli_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def fake_resume(**kwargs):
        calls.append(("resume", kwargs))
        return 2

    async def fake_rebuild(**kwargs):
        calls.append(("rebuild", kwargs))
        return 5

    monkeypatch.setattr(cli, "ingest_pending", fake_resume)
    monkeypatch.setattr(cli, "rebuild_collection", fake_rebuild)

    resume = cli._parser().parse_args(
        ["resume", "--collection", "riskmind_knowledge_v003", "--batch-size", "2"]
    )
    rebuild = cli._parser().parse_args(
        ["rebuild", "--collection", "riskmind_knowledge_v004", "--batch-size", "4"]
    )

    assert asyncio.run(cli._run(resume)) == {"ready": 2}
    assert asyncio.run(cli._run(rebuild)) == {"upserted": 5}
    assert calls == [
        ("resume", {"collection": "riskmind_knowledge_v003", "batch_size": 2}),
        ("rebuild", {"collection": "riskmind_knowledge_v004", "batch_size": 4}),
    ]
