import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from copilot import telemetry
from copilot.core import Copilot
from copilot.limits import Limiter, client_ip

WEB_DIST = Path("web/dist")


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


class Citation(BaseModel):
    num: str
    url: str
    source: str
    date_debut_vigueur: str = ""


class ToolCallOut(BaseModel):
    name: str
    args: dict
    nums: list[str]


class AskResponse(BaseModel):
    answer: str
    citations: list[Citation]
    tool_calls: list[ToolCallOut]
    trace_id: str


LIMIT_MESSAGES = {
    "ip": "Trop de questions en une minute, réessayez dans un instant.",
    "daily": "La limite quotidienne de questions est atteinte, réessayez demain.",
}


def create_app(
    copilot_factory=Copilot.from_env, limiter: Limiter | None = None, web_dist: Path = WEB_DIST
) -> FastAPI:
    # Before FastAPI() so the distro's FastAPI instrumentation sees this app.
    telemetry.setup()
    limiter = limiter or Limiter(daily_cap=int(os.environ.get("DAILY_QUESTION_CAP", "300")))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.copilot = copilot_factory()
        yield
        if hasattr(app.state.copilot, "aclose"):
            await app.state.copilot.aclose()

    app = FastAPI(title="Droit du Travail Copilot", lifespan=lifespan)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/ask", response_model=AskResponse)
    async def ask(body: AskRequest, request: Request) -> AskResponse:
        ip = client_ip(request.headers.get("x-forwarded-for"), getattr(request.client, "host", None))
        refused = limiter.check(ip)
        if refused:
            raise HTTPException(status_code=429, detail=LIMIT_MESSAGES[refused])
        result = await request.app.state.copilot.ask(body.question)
        return AskResponse(**result.to_dict())

    # The React page (web/, built by `npm run build`); mounted last so /ask and /health win.
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app


app = create_app()
