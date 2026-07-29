"""
Pydantic request / response models.

`ResumeAnalysis` is intentionally permissive (`extra="allow"`) — it mirrors the
extraction engine's output, and we only read a handful of fields for matching,
so we never want an unexpected key to reject the payload.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ── Structured resume (subset of the extraction engine output) ──────────────

class ResumeAnalysis(BaseModel):
    model_config = ConfigDict(extra="allow")

    professional_summary: str | None = None
    objective: str | None = None
    work_experience: list[dict[str, Any]] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)
    skills: dict[str, Any] | None = None
    certifications: list[dict[str, Any]] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)
    analytics: dict[str, Any] | None = None


# ── Job description ─────────────────────────────────────────────────────────

class JobInput(BaseModel):
    """A job to match against. Supply structured fields, a free-text blob, or
    both — whatever is available. `requirements`/`responsibilities` accept a
    string (rich-text/HTML or plain) or a list of strings."""
    model_config = ConfigDict(extra="allow")

    title: str | None = None
    description: str | None = None
    requirements: str | list[str] | None = None
    responsibilities: str | list[str] | None = None
    skills: list[str] | None = None
    location: str | None = None
    employment_type: str | None = None
    seniority: str | None = None


# ── /embed ──────────────────────────────────────────────────────────────────

class EmbedRequest(BaseModel):
    resume_id: str = Field(..., description="Stable unique id for this resume (S3 key, application id, …)")
    candidate_name: str | None = None
    analysis: ResumeAnalysis | None = Field(None, description="Structured resume; provide this OR `text`")
    text: str | None = Field(None, description="Raw resume text, if no structured analysis is available")
    source: str | None = Field(None, description="Optional tag: 'application' | 'bank' | …")


class EmbedResponse(BaseModel):
    success: bool = True
    resume_id: str
    dim: int
    stored: bool


# ── /match ──────────────────────────────────────────────────────────────────

class MatchRequest(BaseModel):
    job: JobInput
    top_k: int | None = Field(None, description="How many ranked candidates to return (defaults to server config)")
    pool: int | None = Field(None, description="How many nearest neighbours to shortlist before re-ranking")


class Candidate(BaseModel):
    resume_id: str
    candidate_name: str | None = None
    fit_score: int = Field(..., description="0–100 blended score (semantic similarity + LLM judgment)")
    similarity: float = Field(..., description="Raw cosine similarity, 0–1")
    qualified: bool
    verdict: str = Field(..., description="strong | possible | weak")
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    rationale: str | None = None


class MatchResponse(BaseModel):
    success: bool = True
    count: int
    candidates: list[Candidate]


# ── /score (single resume vs a job) ─────────────────────────────────────────

class ScoreRequest(BaseModel):
    job: JobInput
    resume_id: str | None = Field(None, description="Use a stored resume by id …")
    analysis: ResumeAnalysis | None = Field(None, description="… or pass the structured resume inline")
    text: str | None = Field(None, description="… or raw resume text")
    candidate_name: str | None = None


class ScoreResponse(BaseModel):
    success: bool = True
    resume_id: str | None = None
    candidate_name: str | None = None
    fit_score: int
    qualified: bool
    verdict: str
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    rationale: str | None = None
