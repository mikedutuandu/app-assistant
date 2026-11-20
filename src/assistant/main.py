import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from assistant.api import chat, deps, turn
from assistant.auth import Session
from assistant.config import Settings, get_settings
from assistant.db import close_pool, migrate, open_pool
from assistant.demo import cleanup as demo_cleanup
from assistant.demo import routes as demo_routes
from assistant.demo import tools as demo_tools
from assistant.files.storage import Storage
from assistant.graph.build import serializer
from assistant.llm import Llm
from assistant.packs import AppPack, PackError, load_packs
from assistant.tools import McpHttpTools


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not settings.run_lifespan:
            yield
            return
        packs = load_packs(settings.packs_dir, settings.public_base_url)
        if not packs:
            raise PackError(f"no app packs in {settings.packs_dir}")
        for pack in packs.values():
            pack.secret()  # fail at startup, not on every request, when a pack's secret is unset
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
        http = httpx.AsyncClient()  # one client for OpenRouter, every app's MCP and pasted links
        try:
            await migrate(pool)
            checkpointer = AsyncPostgresSaver(saver_pool, serde=serializer())  # type: ignore[arg-type]
            await checkpointer.setup()
            app.state.packs = packs
            app.state.http = http
            app.state.pool = pool
            app.state.checkpointer = checkpointer
            app.state.storage = Storage(settings)
            app.state.llm = Llm(settings.openrouter_api_key, http, referer=settings.public_base_url)

            def tools_for(pack: AppPack, session: Session) -> McpHttpTools:
                return McpHttpTools(pack, session, http)

            app.state.tools_for = tools_for
            cleanup_task = asyncio.create_task(demo_cleanup.cleanup_loop(app.state))
            try:
                yield
            finally:
                await demo_cleanup.stop(cleanup_task)
            await turn.drain()  # running turns finish and charge before their pools close
        finally:
            await close_pool(saver_pool)
            await close_pool(pool)
            await http.aclose()

    app = FastAPI(title="app-assistant", lifespan=lifespan)
    app.state.settings = settings
    deps.install(app)
    app.include_router(chat.router)
    app.include_router(chat.workspaces)
    app.include_router(demo_routes.router)
    app.include_router(demo_tools.router)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    return app
