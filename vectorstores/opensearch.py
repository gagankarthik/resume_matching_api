"""
OpenSearch (Serverless) vector store — approximate k-NN for large banks.

Search is sub-linear (HNSW), so it stays fast at 100k+ resumes and never loads
the whole set into the Lambda. Imports are lazy so the DynamoDB path doesn't need
opensearch-py installed. Enable with VECTOR_BACKEND=opensearch + OPENSEARCH_ENDPOINT.
"""
from __future__ import annotations

import asyncio
import logging

from config import get_settings
from vectorstores.base import QueryHit, StoredResume, VectorStore

logger = logging.getLogger("matching.store.opensearch")


class OpenSearchVectorStore(VectorStore):
    def __init__(self) -> None:
        self.settings = get_settings()
        self.index = self.settings.opensearch_index
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3
            from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

            endpoint = self.settings.opensearch_endpoint
            if not endpoint:
                raise RuntimeError("OPENSEARCH_ENDPOINT is not configured.")
            host = endpoint.replace("https://", "").replace("http://", "").rstrip("/")

            credentials = boto3.Session().get_credentials()
            auth = AWSV4SignerAuth(credentials, self.settings.aws_region, self.settings.opensearch_service)
            self._client = OpenSearch(
                hosts=[{"host": host, "port": 443}],
                http_auth=auth,
                use_ssl=True,
                verify_certs=True,
                connection_class=RequestsHttpConnection,
                pool_maxsize=20,
            )
        return self._client

    # ── index management ────────────────────────────────────────────────────
    def _ensure_index_sync(self) -> None:
        client = self._get_client()
        if client.indices.exists(index=self.index):
            return
        body = {
            "settings": {"index": {"knn": True}},
            "mappings": {
                "properties": {
                    "vector": {
                        "type": "knn_vector",
                        "dimension": self.settings.embedding_dim,
                        "method": {
                            "name": "hnsw",
                            "space_type": "cosinesimil",
                            "engine": "faiss",
                        },
                    },
                    "candidateName": {"type": "text"},
                    "summary": {"type": "text"},
                    "skills": {"type": "keyword"},
                    "source": {"type": "keyword"},
                }
            },
        }
        client.indices.create(index=self.index, body=body)
        logger.info("created OpenSearch index %s", self.index)

    async def ensure_ready(self) -> None:
        await asyncio.to_thread(self._ensure_index_sync)

    # ── writes ──────────────────────────────────────────────────────────────
    async def upsert(self, record: StoredResume) -> None:
        await self.ensure_ready()
        body = {
            "vector": record.vector,
            "candidateName": record.candidate_name or "",
            "summary": record.summary or "",
            "skills": record.skills or [],
            "source": record.source or "",
        }
        await asyncio.to_thread(
            lambda: self._get_client().index(index=self.index, id=record.resume_id, body=body)
        )

    async def delete(self, resume_id: str) -> bool:
        def _del() -> bool:
            client = self._get_client()
            try:
                client.delete(index=self.index, id=resume_id)
                return True
            except Exception:  # noqa: BLE001 — treat "not found" as "didn't exist"
                return False

        return await asyncio.to_thread(_del)

    async def get(self, resume_id: str) -> StoredResume | None:
        def _get() -> StoredResume | None:
            client = self._get_client()
            # Search by _id — works on both managed OpenSearch and Serverless.
            resp = client.search(
                index=self.index,
                body={"size": 1, "query": {"ids": {"values": [resume_id]}}},
            )
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                return None
            src = hits[0].get("_source", {})
            return StoredResume(
                resume_id=resume_id,
                vector=list(src.get("vector") or []),
                candidate_name=src.get("candidateName") or None,
                summary=src.get("summary") or "",
                skills=list(src.get("skills") or []),
                source=src.get("source") or None,
            )

        return await asyncio.to_thread(_get)

    # ── query ───────────────────────────────────────────────────────────────
    async def query(self, vector: list[float], top_k: int) -> list[QueryHit]:
        def _search() -> list[QueryHit]:
            client = self._get_client()
            body = {
                "size": top_k,
                "query": {"knn": {"vector": {"vector": vector, "k": top_k}}},
                "_source": ["candidateName", "summary", "skills"],
            }
            resp = client.search(index=self.index, body=body)
            out: list[QueryHit] = []
            for h in resp.get("hits", {}).get("hits", []):
                src = h.get("_source", {})
                # faiss cosinesimil _score is in (0,1]; clamp for safety.
                score = float(h.get("_score") or 0.0)
                out.append(
                    QueryHit(
                        resume_id=h.get("_id"),
                        similarity=max(0.0, min(1.0, score)),
                        candidate_name=src.get("candidateName") or None,
                        summary=src.get("summary") or "",
                        skills=list(src.get("skills") or []),
                    )
                )
            return out

        return await asyncio.to_thread(_search)
