"""Shared warm HTTP/2 clients — one per provider, created once (Plan §4.2.1)."""

import asyncio
import os

import httpx

_CLIENTS: dict[str, httpx.AsyncClient] = {}


def client(name, base_url=None, timeout=25.0):
    existing = _CLIENTS.get(name)
    if existing is None or existing.is_closed:
        existing = httpx.AsyncClient(
            http2=True,
            timeout=timeout,
            base_url=base_url or "",
            limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=600),
        )
        _CLIENTS[name] = existing
    return existing


def typesafe_client():
    return client("typesafe", os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai"))


def inception_client():
    return client("inception", os.environ.get("INCEPTION_BASE_URL", "https://api.inceptionlabs.ai/v1"))


async def warm():
    """Establish TLS + HTTP/2 to each provider before the first real request.

    A HEAD to the base URL is enough to pay DNS/TLS/ALPN once; failures are
    ignored because warm-up must never be able to stop the agent starting.
    """

    async def ping(name, url):
        try:
            await client(name).head(url, timeout=5.0)
        except Exception:
            pass

    targets = [
        ("typesafe", os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai")),
        ("inception", os.environ.get("INCEPTION_BASE_URL", "https://api.inceptionlabs.ai/v1")),
    ]
    await asyncio.gather(*(ping(name, url) for name, url in targets))


async def close_all():
    for name, existing in list(_CLIENTS.items()):
        await existing.aclose()
        _CLIENTS.pop(name, None)
