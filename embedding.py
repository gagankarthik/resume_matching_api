"""
OpenAI embeddings client. Singleton AsyncOpenAI (one connection pool), retry
with backoff, and batching. Uses text-embedding-3-small (1536 dims) by default.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from config import get_settings

logger = logging.getLogger("matching.embedding")

_client: Any = None

# Parse OpenAI's "try again in 704ms" hint from 429 responses.
_RETRY_HINT_RE = re.compile(r"try again in (\d+(?:\.\d+)?)\s*(ms|s|second|seconds)", re.IGNORECASE)


def _parse_retry_after(exc: Exception) -> float | None:
    m = _RETRY_HINT_RE.search(str(exc))
    if not m:
        return None
    value = float(m.group(1))
    return value / 1000.0 if m.group(2).lower() == "ms" else value


def _get_client() -> Any:
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        settings = get_settings()
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        kwargs: dict[str, Any] = {"api_key": settings.openai_api_key}
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        _client = AsyncOpenAI(**kwargs)
    return _client


async def embed_texts(texts: list[str], *, retries: int = 5) -> list[list[float]]:
    """Embed a batch of texts. Empty/blank strings are embedded as a single
    space so the vectors stay aligned with the input list."""
    if not texts:
        return []
    settings = get_settings()
    client = _get_client()
    payload = [t if (t and t.strip()) else " " for t in texts]

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.embeddings.create(model=settings.embedding_model, input=payload)
            # resp.data is returned in input order, but sort by index to be safe.
            ordered = sorted(resp.data, key=lambda d: d.index)
            return [d.embedding for d in ordered]
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries:
                hinted = _parse_retry_after(exc)
                wait = (hinted + 0.1 * (attempt + 1)) if hinted is not None else min(30.0, 2 ** attempt)
                logger.warning("embedding attempt %d failed: %s — retrying in %.2fs", attempt + 1, exc, wait)
                await asyncio.sleep(wait)
    raise RuntimeError(f"All embedding attempts failed: {last_exc}") from last_exc


async def embed_text(text: str) -> list[float]:
    result = await embed_texts([text])
    return result[0]
