import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from copilot import telemetry
from copilot.core import Copilot
from copilot.limits import Limiter, client_ip, limiter_from_env

WEB_DIST = Path("web/dist")
MAX_BODY_BYTES = 16 * 1024  # a 2,000-character question is under 8 KB of JSON
DISCONNECT_POLL_S = 1.0

# The page loads only its own script, stylesheet and font; Légifrance links are plain anchors.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; font-src 'self' data:; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Strict-Transport-Security": "max-age=31536000",
}


TOO_LARGE = "Requête trop volumineuse."


class BodyTooLarge(HTTPException):
    """An HTTPException so FastAPI's body parsing re-raises it instead of answering 400."""

    def __init__(self):
        super().__init__(status_code=413, detail=TOO_LARGE)


class GuardMiddleware:
    """Refuses request bodies over `max_body` bytes (by Content-Length, and by counting chunks
    for bodies sent without one) and adds the security headers to every response."""

    def __init__(self, app, max_body: int = MAX_BODY_BYTES):
        self.app, self.max_body = app, max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > self.max_body):
            return await self._too_large(scope, receive, send)

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body:
                    raise BodyTooLarge
            return message

        started = False

        async def send_with_headers(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                extra = [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except BodyTooLarge:
            if not started:
                await self._too_large(scope, receive, send_with_headers)

    async def _too_large(self, scope, receive, send):
        resp = JSONResponse({"detail": TOO_LARGE}, status_code=413)
        await resp(scope, receive, send)


async def run_unless_disconnected(request: Request, coro, poll_s: float = DISCONNECT_POLL_S):
    """Run the agent, cancelling it when the caller goes away so no more tokens are spent."""
    task = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll_s)
            if done:
                return task.result()
            if await request.is_disconnected():
                task.cancel()
                raise HTTPException(status_code=499, detail="Client parti.")
    finally:
        if not task.done():
            task.cancel()


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
    copilot_factory=Copilot.from_env,
    limiter: Limiter | None = None,
    web_dist: Path = WEB_DIST,
    show_docs: bool | None = None,
) -> FastAPI:
    # Before FastAPI() so the distro's FastAPI instrumentation sees this app.
    telemetry.setup()
    limiter = limiter or limiter_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.copilot = copilot_factory()
        yield
        if hasattr(app.state.copilot, "aclose"):
            await app.state.copilot.aclose()
        await limiter.aclose()

    # Container Apps sets CONTAINER_APP_NAME; the public site exposes no API docs.
    docs = show_docs if show_docs is not None else "CONTAINER_APP_NAME" not in os.environ
    app = FastAPI(
        title="Droit du Travail Copilot",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.add_middleware(GuardMiddleware)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/ask", response_model=AskResponse)
    async def ask(body: AskRequest, request: Request) -> AskResponse:
        ip = client_ip(
            request.headers.get("x-forwarded-for"), getattr(request.client, "host", None)
        )
        refused = await limiter.check(ip)
        if refused:
            raise HTTPException(status_code=429, detail=LIMIT_MESSAGES[refused])
        result = await run_unless_disconnected(
            request, request.app.state.copilot.ask(body.question)
        )
        return AskResponse(**result.to_dict())

    # The React page (web/, built by `npm run build`); mounted last so /ask and /health win.
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app


app = create_app()
