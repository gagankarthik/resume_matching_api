"""
DynamoDB vector store — brute-force cosine similarity for small→medium banks.

Vectors are stored as compact float32 bytes (~6 KB for 1536 dims). On a query we
load every vector into a NumPy matrix and do one matrix-multiply. Warm Lambda
invocations cache the matrix in memory with a TTL so we don't re-scan the table
on every request. Good to ~10k–20k resumes; switch to OpenSearch beyond that.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np

from config import get_settings
from vectorstores.base import QueryHit, StoredResume, VectorStore

logger = logging.getLogger("matching.store.dynamo")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_bytes(vector: list[float]) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def _from_bytes(raw: Any) -> np.ndarray:
    # boto3 returns a Binary wrapper; unwrap to raw bytes.
    data = raw.value if hasattr(raw, "value") else bytes(raw)
    return np.frombuffer(data, dtype=np.float32)


class DynamoVectorStore(VectorStore):
    def __init__(self) -> None:
        self.settings = get_settings()
        self._table = None
        # In-memory cache of the whole table for brute-force search.
        self._cache: dict[str, Any] = {"ts": 0.0, "ids": [], "matrix": None, "meta": {}}

    # ── boto3 lazy init ─────────────────────────────────────────────────────
    def _get_table(self):
        if self._table is None:
            import boto3

            resource = boto3.resource("dynamodb", region_name=self.settings.aws_region)
            self._table = resource.Table(self.settings.ddb_table)
        return self._table

    async def ensure_ready(self) -> None:
        # Table is created by Terraform; nothing to do at runtime.
        return

    def _invalidate(self) -> None:
        self._cache = {"ts": 0.0, "ids": [], "matrix": None, "meta": {}}

    # ── writes ──────────────────────────────────────────────────────────────
    async def upsert(self, record: StoredResume) -> None:
        item = {
            "resumeId": record.resume_id,
            "vec": _to_bytes(record.vector),
            "candidateName": record.candidate_name or "",
            "summary": record.summary or "",
            "skills": record.skills or [],
            "source": record.source or "",
            "updatedAt": _now_iso(),
        }
        await asyncio.to_thread(self._get_table().put_item, Item=item)
        self._invalidate()

    async def delete(self, resume_id: str) -> bool:
        existing = await self.get(resume_id)
        await asyncio.to_thread(self._get_table().delete_item, Key={"resumeId": resume_id})
        self._invalidate()
        return existing is not None

    async def exists(self, resume_id: str) -> bool:
        # Projection to just the key — avoids reading the ~6 KB vector.
        resp = await asyncio.to_thread(
            self._get_table().get_item,
            Key={"resumeId": resume_id},
            ProjectionExpression="resumeId",
        )
        return "Item" in resp

    async def get(self, resume_id: str) -> StoredResume | None:
        resp = await asyncio.to_thread(self._get_table().get_item, Key={"resumeId": resume_id})
        item = resp.get("Item")
        if not item:
            return None
        return StoredResume(
            resume_id=item["resumeId"],
            vector=_from_bytes(item["vec"]).tolist(),
            candidate_name=item.get("candidateName") or None,
            summary=item.get("summary") or "",
            skills=list(item.get("skills") or []),
            source=item.get("source") or None,
        )

    # ── read-all (with cache) ───────────────────────────────────────────────
    def _scan_all(self) -> tuple[list[str], np.ndarray, dict[str, dict[str, Any]]]:
        table = self._get_table()
        ids: list[str] = []
        vectors: list[np.ndarray] = []
        meta: dict[str, dict[str, Any]] = {}
        kwargs: dict[str, Any] = {
            "ProjectionExpression": "resumeId, vec, candidateName, summary, skills",
        }
        while True:
            resp = table.scan(**kwargs)
            for item in resp.get("Items", []):
                rid = item["resumeId"]
                ids.append(rid)
                vectors.append(_from_bytes(item["vec"]))
                meta[rid] = {
                    "candidate_name": item.get("candidateName") or None,
                    "summary": item.get("summary") or "",
                    "skills": list(item.get("skills") or []),
                }
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

        if vectors:
            matrix = np.vstack(vectors).astype(np.float32)
            # L2-normalise rows once so cosine == dot product at query time.
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            matrix = matrix / norms
        else:
            matrix = np.zeros((0, self.settings.embedding_dim), dtype=np.float32)
        return ids, matrix, meta

    async def _load(self) -> tuple[list[str], np.ndarray, dict[str, dict[str, Any]]]:
        fresh = (time.time() - self._cache["ts"]) < self.settings.ddb_cache_ttl
        if fresh and self._cache["matrix"] is not None:
            return self._cache["ids"], self._cache["matrix"], self._cache["meta"]
        ids, matrix, meta = await asyncio.to_thread(self._scan_all)
        self._cache = {"ts": time.time(), "ids": ids, "matrix": matrix, "meta": meta}
        return ids, matrix, meta

    # ── query ───────────────────────────────────────────────────────────────
    async def query(self, vector: list[float], top_k: int) -> list[QueryHit]:
        ids, matrix, meta = await self._load()
        if matrix.shape[0] == 0:
            return []

        q = np.asarray(vector, dtype=np.float32)
        qn = np.linalg.norm(q)
        if qn == 0:
            return []
        q = q / qn

        sims = matrix @ q  # cosine similarity, since both sides are normalised
        k = min(top_k, sims.shape[0])
        # Top-k via argpartition, then sort just those.
        top_idx = np.argpartition(-sims, k - 1)[:k]
        top_idx = top_idx[np.argsort(-sims[top_idx])]

        hits: list[QueryHit] = []
        for i in top_idx:
            rid = ids[i]
            m = meta.get(rid, {})
            hits.append(
                QueryHit(
                    resume_id=rid,
                    similarity=float(max(0.0, min(1.0, sims[i]))),
                    candidate_name=m.get("candidate_name"),
                    summary=m.get("summary", ""),
                    skills=list(m.get("skills") or []),
                )
            )
        return hits
