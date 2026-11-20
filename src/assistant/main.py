from fastapi import FastAPI

from assistant.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="app-assistant")
    app.state.settings = settings or get_settings()

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    return app
