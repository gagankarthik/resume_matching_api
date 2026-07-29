"""Vector store factory — pick the backend from VECTOR_BACKEND."""
from __future__ import annotations

from functools import lru_cache

from config import get_settings
from vectorstores.base import QueryHit, StoredResume, VectorStore

__all__ = ["QueryHit", "StoredResume", "VectorStore", "get_store"]


@lru_cache(maxsize=1)
def get_store() -> VectorStore:
    backend = get_settings().vector_backend
    if backend == "opensearch":
        from vectorstores.opensearch import OpenSearchVectorStore

        return OpenSearchVectorStore()
    if backend == "dynamodb":
        from vectorstores.dynamo import DynamoVectorStore

        return DynamoVectorStore()
    raise ValueError(f"Unknown VECTOR_BACKEND '{backend}' (expected 'dynamodb' or 'opensearch').")
