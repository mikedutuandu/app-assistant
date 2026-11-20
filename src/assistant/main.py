from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from assistant.config import Settings, get_settings
from assistant.db import close_pool, migrate, open_pool


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not settings.run_lifespan:
            yield
            return
        pool = await open_pool(settings.database_url)
        # LangGraph's saver needs autocommit connections and dict rows, so it gets its own pool.
        saver_pool = AsyncConnectionPool(
            settings.database_url,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await saver_pool.open()
        try:
            await migrate(pool)
            checkpointer = AsyncPostgresSaver(saver_pool)  # type: ignore[arg-type]
            await checkpointer.setup()
            app.state.pool = pool
            app.state.checkpointer = checkpointer
            yield
        finally:
            await close_pool(saver_pool)
            await close_pool(pool)

    app = FastAPI(title="app-assistant", lifespan=lifespan)
    app.state.settings = settings

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    return app
