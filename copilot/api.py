from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from copilot.core import Copilot


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


def create_app(copilot_factory=Copilot.from_env) -> FastAPI:
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
        result = await request.app.state.copilot.ask(body.question)
        return AskResponse(**result.to_dict())

    return app


app = create_app()
