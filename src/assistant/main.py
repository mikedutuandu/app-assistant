import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Response
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from assistant.api import chat, deps, internal, turn
from assistant.auth import Session
from assistant.config import Settings, get_settings
from assistant.db import close_pool, direct_url, migrate, open_pool, open_saver_pool
from assistant.demo import routes as demo_routes
from assistant.demo import tools as demo_tools
from assistant.files.storage import Storage
from assistant.graph.build import serializer
from assistant.llm import Llm
from assistant.packs import AppPack, PackError, load_packs
from assistant.tools import McpHttpTools

WIDGET_JS = Path(__file__).parent / "static" / "widget.js"  # built by `npm run build` in widget/
LOG_FORMAT = "%(levelname)s %(name)s %(message)s"
log = logging.getLogger("assistant.main")


class _StdoutHandler(logging.StreamHandler):
    """Marks the one handler this module owns, so a second create_app replaces it instead of adding another."""


def log_to_stdout() -> None:
    """The service's own lines (ids, timings, costs, error kinds) at INFO on stdout, where the host reads them.

    Only the "assistant" logger, which does not propagate: the root logger is left alone, so libraries such as
    httpx (which logs every URL it calls) stay at their quiet defaults.
    """
    logger = logging.getLogger("assistant")
    for old in [h for h in logger.handlers if isinstance(h, _StdoutHandler)]:
        logger.removeHandler(old)
    handler = _StdoutHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


async def set_bucket_cors(storage: Storage, packs: dict[str, AppPack], settings: Settings) -> None:
    """The browser PUTs files straight to the bucket: allow every pack's origins and the demo's. Never stops startup."""
    found = [o for pack in packs.values() for o in pack.allowed_origins] + [settings.public_base_url]
    origins = []
    for value in found:  # a pack's {base} carries the demo's path: CORS wants the origin alone
        parts = urlsplit(value)
        origins.append(f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else value)
    try:
        await asyncio.to_thread(storage.ensure_cors, origins)
    except Exception as e:  # noqa: BLE001 - a CORS failure must not take the bot down
        logging.getLogger("assistant.storage").warning("could not set the bucket's CORS: %s", e)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    log_to_stdout()

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
        pool = await open_pool(direct_url(settings), settings.db_schema, settings.db_pool_max)
        saver_pool = await open_saver_pool(direct_url(settings), settings.db_schema, settings.saver_pool_max)
        http = httpx.AsyncClient()  # one client for OpenRouter, every app's MCP and pasted links
        try:
            checkpointer = AsyncPostgresSaver(saver_pool, serde=serializer())  # type: ignore[arg-type]
            await migrate(pool, settings.db_schema, after=checkpointer.setup)  # both under one advisory lock
            app.state.packs = packs
            app.state.http = http
            app.state.pool = pool
            app.state.checkpointer = checkpointer
            app.state.storage = Storage(settings)
            log.info("uploads: %s", app.state.storage.upload_backend)  # a missing SUPABASE_* shows here
            if app.state.storage.upload_backend == "s3":
                await set_bucket_cors(app.state.storage, packs, settings)
            app.state.llm = Llm(settings.openrouter_api_key, http, referer=settings.public_base_url)

            def tools_for(pack: AppPack, session: Session) -> McpHttpTools:
                return McpHttpTools(pack, session, http)

            app.state.tools_for = tools_for
            yield
            await turn.drain(
                settings.shutdown_drain_seconds
            )  # running turns finish and charge before their pools close
        finally:
            await close_pool(saver_pool)
            await close_pool(pool)
            await http.aclose()

    app = FastAPI(title="app-assistant", lifespan=lifespan)
    app.state.settings = settings
    deps.install(app)
    app.include_router(internal.router)
    app.include_router(chat.router)
    app.include_router(chat.workspaces)
    app.include_router(demo_routes.router)
    app.include_router(demo_tools.router)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/widget.js", include_in_schema=False)
    async def widget() -> Response:
        if not WIDGET_JS.is_file():
            return Response(status_code=404)
        return Response(
            WIDGET_JS.read_bytes(),
            media_type="application/javascript",
            headers={"Cache-Control": "public, max-age=300"},
        )

    return app
