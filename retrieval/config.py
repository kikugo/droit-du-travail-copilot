import os
from dataclasses import dataclass
from functools import cached_property

from dotenv import load_dotenv

COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


@dataclass(frozen=True)
class Settings:
    search_endpoint: str
    openai_endpoint: str
    embedding_deployment: str = "text-embedding-3-small"
    embedding_dims: int = 512
    search_index: str = "articles-v1"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            search_endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
            openai_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            embedding_deployment=os.environ.get("EMBEDDING_DEPLOYMENT", cls.embedding_deployment),
            embedding_dims=int(os.environ.get("EMBEDDING_DIMS", cls.embedding_dims)),
            search_index=os.environ.get("SEARCH_INDEX", cls.search_index),
        )


class Clients:
    """Entra ID clients built lazily from Settings; no API keys anywhere."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @cached_property
    def credential(self):
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential()

    @cached_property
    def openai(self):
        from azure.identity import get_bearer_token_provider
        from openai import OpenAI

        base_url = self.settings.openai_endpoint.rstrip("/") + "/openai/v1/"
        return OpenAI(
            base_url=base_url, api_key=get_bearer_token_provider(self.credential, COGNITIVE_SCOPE)
        )

    @cached_property
    def index_client(self):
        from azure.search.documents.indexes import SearchIndexClient

        return SearchIndexClient(self.settings.search_endpoint, self.credential)

    @cached_property
    def search_client(self):
        from azure.search.documents import SearchClient

        return SearchClient(
            self.settings.search_endpoint, self.settings.search_index, self.credential
        )
