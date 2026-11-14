"""Loopback-only local API launcher; Windows psycopg needs a Selector loop."""
import asyncio
import uvicorn


def main():
    config = uvicorn.Config("risk_platform.bootstrap:create_local_app", factory=True,
                            host="127.0.0.1", port=8000, lifespan="on")
    # Calling Server.serve inside our loop avoids Uvicorn choosing Proactor on Windows.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
