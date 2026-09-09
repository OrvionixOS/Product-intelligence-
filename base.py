from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ProviderEnvelope:
    provider: str
    provider_version: str | None
    retrieved_at: str
    collection_method: str
    geography: str | None
    language: str | None
    source_reference: str | None
    raw_payload: dict[str, Any]
    limitations: list[str]


class SearchDemandProvider(ABC):
    @abstractmethod
    async def research(self, queries: list[str], geography: str, language: str) -> ProviderEnvelope:
        raise NotImplementedError


class MarketplaceProvider(ABC):
    @abstractmethod
    async def research(self, query: str, geography: str, language: str) -> ProviderEnvelope:
        raise NotImplementedError


class PublicContentProvider(ABC):
    @abstractmethod
    async def research(self, query: str, geography: str, language: str) -> ProviderEnvelope:
        raise NotImplementedError
