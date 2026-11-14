"""Explicit checkpoint-table setup only; never migrate application tables."""
import argparse
import asyncio
from risk_platform.db.database import get_database_url
from risk_platform.workflow.checkpoints import postgres_checkpointer


async def setup():
    async with postgres_checkpointer(get_database_url(), setup=True):
        pass
    print("workflow_checkpoint_setup: PASS")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True,
                        help="Explicitly create/upgrade checkpoint tables in RISK_DATABASE_URL")
    parser.parse_args()
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(setup())


if __name__ == "__main__":
    main()
