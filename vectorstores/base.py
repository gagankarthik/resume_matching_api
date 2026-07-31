"""
Vector store interface. Two implementations plug in behind it (DynamoDB
brute-force for small banks, OpenSearch k-NN for large ones); the matcher code
only ever sees this interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StoredResume:
    """One resume's record in the vector store.

    `source` and `owner` are what keep unrelated banks apart in one table: an
    application tags what it writes (`source`), and where it has a signed-in
    user, who wrote it (`owner`). A scoped query then sees only its own
    records. Anything stored before scoping existed carries neither tag, so it
    matches no scoped query — the safe direction to fail."""
    resume_id: str
    vector: list[float]
    candidate_name: str | None = None
    summary: str = ""
    skills: list[str] = field(default_factory=list)
    source: str | None = None
    owner: str | None = None


def scope_mask(
    ids: list[str],
    meta: dict[str, dict[str, Any]],
    source: str | None,
    owner: str | None,
) -> list[bool]:
    """Which stored resumes a scoped query is allowed to see.

    Lives here, in the dependency-free layer, so the rule that separates one
    application's bank from another's can be read and tested on its own.
    """
    if not source and not owner:
        return [True] * len(ids)
    allowed: list[bool] = []
    for rid in ids:
        m = meta.get(rid) or {}
        allowed.append(
            (not source or (m.get("source") or "") == source)
            and (not owner or (m.get("owner") or "") == owner)
        )
    return allowed


@dataclass
class QueryHit:
    resume_id: str
    similarity: float  # cosine similarity, 0–1
    candidate_name: str | None = None
    summary: str = ""
    skills: list[str] = field(default_factory=list)


class VectorStore(ABC):
    @abstractmethod
    async def ensure_ready(self) -> None:
        """Create the index/table if needed. Cheap to call repeatedly."""

    @abstractmethod
    async def upsert(self, record: StoredResume) -> None:
        """Insert or replace a resume's vector + metadata."""

    @abstractmethod
    async def query(
        self,
        vector: list[float],
        top_k: int,
        *,
        source: str | None = None,
        owner: str | None = None,
    ) -> list[QueryHit]:
        """Return the top_k nearest resumes by cosine similarity.

        `source`/`owner` narrow the candidate set *before* the ranking is cut,
        so a scoped caller gets its own top_k rather than whatever survives a
        filter applied afterwards."""

    @abstractmethod
    async def get(self, resume_id: str) -> StoredResume | None:
        """Fetch one stored resume, or None if absent."""

    @abstractmethod
    async def delete(self, resume_id: str) -> bool:
        """Remove a resume. Returns True if it existed."""

    async def exists(self, resume_id: str) -> bool:
        """Cheap presence check (used for idempotent ingest). Backends may
        override with a projection/count that avoids fetching the vector."""
        return (await self.get(resume_id)) is not None

    async def exists_many(self, resume_ids: list[str]) -> dict[str, bool]:
        """Batch presence check. Default loops; backends may override with a
        single batched read."""
        out: dict[str, bool] = {}
        for rid in resume_ids:
            out[rid] = await self.exists(rid)
        return out
