"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from zakupator.models import Address

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def moscow_address() -> Address:
    return Address(label="Тест", text="Москва", lat=55.7558, lon=37.6173)


@pytest.fixture
def vkusvill_html() -> str:
    return (FIXTURES_DIR / "vkusvill_search.html").read_text(encoding="utf-8")


@pytest.fixture
def auchan_json() -> bytes:
    return (FIXTURES_DIR / "auchan_autohints.json").read_bytes()


@pytest.fixture
def metro_json() -> bytes:
    return (FIXTURES_DIR / "metro_graphql.json").read_bytes()


def mock_client(
    content: bytes | str,
    *,
    status: int = 200,
    content_type: str = "application/json",
) -> httpx.AsyncClient:
    """Build an AsyncClient whose transport returns a canned response.

    Any request made via this client — regardless of URL or method — gets the
    same body back. Adapters only make one call per `search()`, so there's no
    need for per-URL routing in these tests.
    """
    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = content

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=content_bytes, headers={"content-type": content_type})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))
