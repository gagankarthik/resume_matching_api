"""
Vector store interface. Two implementations plug in behind it (DynamoDB
brute-force for small banks, OpenSearch k-NN for large ones); the matcher code
only ever sees this interface.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class StoredResume:
    """One resume's record in the vector store."""
    resume_id: str
    vector: list[float]
    candidate_name: str | None = None
    summary: str = ""
    skills: list[str] = field(default_factory=list)
    source: str | None = None


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
    async def query(self, vector: list[float], top_k: int) -> list[QueryHit]:
        """Return the top_k nearest resumes by cosine similarity."""

    @abstractmethod
    async def get(self, resume_id: str) -> StoredResume | None:
        """Fetch one stored resume, or None if absent."""

    @abstractmethod
    async def delete(self, resume_id: str) -> bool:
        """Remove a resume. Returns True if it existed."""
