"""
Runtime configuration, read once from environment variables.

Mirrors the extraction engine's convention: a flat settings object loaded from
`.env` locally and from Lambda environment variables in production.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Load .env for local dev; in Lambda the vars come from the function config and
# there is no .env file (load_dotenv is a no-op then).
load_dotenv(Path(__file__).parent / ".env", override=False)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class Settings:
    def __init__(self) -> None:
        # ── OpenAI ──────────────────────────────────────────────────────────
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        # Optional custom base URL (Azure OpenAI / proxy); None → api.openai.com
        self.openai_base_url: str | None = os.getenv("OPENAI_BASE_URL") or None
        self.embedding_model: str = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        self.chat_model: str = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
        # Dimension of the embedding model above (text-embedding-3-small = 1536).
        self.embedding_dim: int = _int("EMBEDDING_DIM", 1536)

        # ── Vector store ───────────────────────────────────────────────────
        # "dynamodb" (default, brute-force cosine) or "opensearch" (k-NN).
        self.vector_backend: str = os.getenv("VECTOR_BACKEND", "dynamodb").strip().lower()
        self.aws_region: str = os.getenv("AWS_REGION", "us-east-2")

        # DynamoDB backend
        self.ddb_table: str = os.getenv("DDB_TABLE", "oceanblue-resume-vectors")
        # How long (seconds) a warm Lambda caches the loaded vector matrix before
        # re-scanning the table. Higher = fewer scans, staler results.
        self.ddb_cache_ttl: int = _int("DDB_CACHE_TTL", 300)

        # OpenSearch backend
        self.opensearch_endpoint: str = os.getenv("OPENSEARCH_ENDPOINT", "").rstrip("/")
        self.opensearch_index: str = os.getenv("OPENSEARCH_INDEX", "resume-vectors")
        # "aoss" for OpenSearch Serverless, "es" for a managed OpenSearch domain.
        self.opensearch_service: str = os.getenv("OPENSEARCH_SERVICE", "aoss")

        # ── Matching pipeline knobs ────────────────────────────────────────
        # How many nearest neighbours to pull from the store before re-ranking.
        self.candidate_pool: int = _int("MATCH_CANDIDATE_POOL", 25)
        # How many final ranked candidates to return from /match.
        self.rerank_top: int = _int("MATCH_RERANK_TOP", 10)
        # Blend of semantic similarity vs LLM judgment in the final score.
        # final = sim_weight * cosine*100 + llm_weight * llm_fit_score
        self.sim_weight: float = _float("MATCH_SIM_WEIGHT", 0.35)
        self.llm_weight: float = _float("MATCH_LLM_WEIGHT", 0.65)

        # ── Auth ───────────────────────────────────────────────────────────
        # Shared secret required in the X-API-Key header. Empty = auth disabled
        # (dev only — always set this in production).
        self.api_key: str = os.getenv("API_KEY", "")

        # ── Existing extraction engine (for /ingest and backfill) ──────────
        self.parser_url: str = os.getenv("RESUME_PARSER_URL", "").rstrip("/")
        self.parser_timeout: float = _float("RESUME_PARSER_TIMEOUT", 120.0)

        # ── Limits ─────────────────────────────────────────────────────────
        self.max_file_mb: int = _int("MAX_FILE_SIZE_MB", 20)
        self.request_timeout: float = _float("REQUEST_TIMEOUT", 60.0)

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
