"""Local control board: `python -m app.api` → http://127.0.0.1:8765 (local only, single user, no auth)."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api.deps import get_container
from app.api.routers import engagement
from app.domain.errors import CampaignNotFound, ConfigMissing, DomainError

STATIC = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="Comment to DM control board", docs_url="/api/docs", redoc_url=None)
    app.include_router(engagement.router)

    @app.exception_handler(DomainError)
    def domain_error(_: Request, e: DomainError) -> JSONResponse:
        code = 404 if isinstance(e, CampaignNotFound) else 400
        detail = str(e) if isinstance(e, ConfigMissing) else get_container().redact(str(e))
        return JSONResponse({"detail": detail}, status_code=code)

    @app.get("/", include_in_schema=False)
    def control_board() -> FileResponse:
        return FileResponse(STATIC / "control.html", headers={"Cache-Control": "no-store"})

    return app


app = create_app()
