"""Légifrance API through PISTE (OAuth 2.0 client credentials).

Endpoints from the DILA "exemples d'utilisation de l'API" guide: POST /consult/getArticle for
LEGIARTI ids, POST /consult/kaliArticle for KALIARTI ids.
"""

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

URLS = {
    "prod": (
        "https://oauth.piste.gouv.fr/api/oauth/token",
        "https://api.piste.gouv.fr/dila/legifrance/lf-engine-app",
    ),
    "sandbox": (
        "https://sandbox-oauth.piste.gouv.fr/api/oauth/token",
        "https://sandbox-api.piste.gouv.fr/dila/legifrance/lf-engine-app",
    ),
}
RETRY_STATUS = {429, 500, 502, 503, 504}


class PisteError(RuntimeError):
    pass


@dataclass(frozen=True)
class LiveArticle:
    id: str
    num: str
    text: str
    date_debut_vigueur: str
    etat: str


def _iso(ms) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, tz=UTC).date().isoformat()


class PisteClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        env: str = "prod",
        http: httpx.Client | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
        retries: int = 3,
    ):
        self.token_url, self.api_url = URLS[env]
        self.client_id, self.client_secret = client_id, client_secret
        self.http = http or httpx.Client(timeout=20)
        self.sleep, self.clock, self.retries = sleep, clock, retries
        self._token: str | None = None
        self._expires_at = 0.0

    @classmethod
    def from_env(cls) -> "PisteClient | None":
        client_id, secret = os.environ.get("PISTE_CLIENT_ID"), os.environ.get("PISTE_CLIENT_SECRET")
        if not client_id or not secret:
            return None
        return cls(client_id, secret, env=os.environ.get("PISTE_ENV", "prod"))

    def _get_token(self) -> str:
        if self._token and self.clock() < self._expires_at - 60:
            return self._token
        resp = self.http.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "openid",
            },
        )
        if resp.status_code != 200:
            raise PisteError(f"token request failed: HTTP {resp.status_code}")
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = self.clock() + float(body.get("expires_in", 3600))
        return self._token

    def _post(self, path: str, payload: dict) -> dict:
        for attempt in range(self.retries + 1):
            resp = self.http.post(
                f"{self.api_url}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {self._get_token()}"},
            )
            if resp.status_code == 401 and attempt < self.retries:
                self._token = None  # expired early; fetch a new one
                continue
            if resp.status_code in RETRY_STATUS and attempt < self.retries:
                self.sleep(float(resp.headers.get("retry-after", 2**attempt)))
                continue
            if resp.status_code != 200:
                raise PisteError(f"{path} failed: HTTP {resp.status_code}")
            return resp.json()
        raise PisteError(f"{path} failed after {self.retries} retries")

    def get_article(self, article_id: str) -> LiveArticle:
        path = (
            "/consult/kaliArticle" if article_id.startswith("KALIARTI") else "/consult/getArticle"
        )
        body = self._post(path, {"id": article_id})
        article = body.get("article") or {}
        text = article.get("texte") or article.get("content") or ""
        if not text:
            raise PisteError(f"{path} returned no article text for {article_id}")
        return LiveArticle(
            id=article.get("id", article_id),
            num=article.get("num") or "",
            text=text.strip(),
            date_debut_vigueur=_iso(article.get("dateDebut")),
            etat=article.get("etat") or "",
        )
