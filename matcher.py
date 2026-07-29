"""
Orchestration: turn requests into store reads/writes + embedding + LLM calls.

    embed_and_store         — /embed
    embed_and_store_analysis— /ingest (after parsing)
    match_job               — /match  (job -> ranked candidates)
    score_one               — /score  (one resume vs a job)
    delete_resume           — DELETE /vectors/{id}
"""
from __future__ import annotations

import logging

import embedding
import llm
import text_builder as tb
from config import get_settings
from models import (
    Candidate,
    EmbedRequest,
    EmbedResponse,
    JobInput,
    ScoreRequest,
    ScoreResponse,
)
from vectorstores import StoredResume, get_store

logger = logging.getLogger("matching.matcher")


# ── writes ──────────────────────────────────────────────────────────────────

async def embed_and_store(req: EmbedRequest) -> EmbedResponse:
    if req.analysis is not None:
        analysis = req.analysis.model_dump()
        return await embed_and_store_analysis(
            resume_id=req.resume_id,
            candidate_name=req.candidate_name,
            analysis=analysis,
            source=req.source or "application",
        )

    # Raw-text path: embed the text directly, keep a truncated summary.
    text = (req.text or "").strip()
    vector = await embedding.embed_text(text)
    store = get_store()
    await store.upsert(
        StoredResume(
            resume_id=req.resume_id,
            vector=vector,
            candidate_name=req.candidate_name,
            summary=text[:1200],
            skills=[],
            source=req.source or "text",
        )
    )
    return EmbedResponse(resume_id=req.resume_id, dim=len(vector), stored=True)


async def embed_and_store_analysis(
    *, resume_id: str, candidate_name: str | None, analysis: dict, source: str | None
) -> EmbedResponse:
    text = tb.build_resume_text(analysis, candidate_name)
    summary = tb.build_resume_summary(analysis, candidate_name)
    skills = tb.resume_skills(analysis)
    vector = await embedding.embed_text(text)

    store = get_store()
    await store.upsert(
        StoredResume(
            resume_id=resume_id,
            vector=vector,
            candidate_name=candidate_name,
            summary=summary,
            skills=skills,
            source=source,
        )
    )
    return EmbedResponse(resume_id=resume_id, dim=len(vector), stored=True)


async def delete_resume(resume_id: str) -> bool:
    return await get_store().delete(resume_id)


async def already_indexed(resume_id: str) -> bool:
    return await get_store().exists(resume_id)


# ── job resolution (two input types) ────────────────────────────────────────

async def _resolve_job(job: JobInput | None, raw_text: str | None) -> dict:
    """Accept either a structured job (integration) or a raw pasted JD blob
    (copy-paste). For pasted text we run one gpt-4.1-mini pass to structure it;
    any explicit structured fields win and the text fills the gaps."""
    job_dict = job.model_dump() if job is not None else {}
    if raw_text and raw_text.strip():
        parsed = await llm.parse_job(raw_text)
        for key, val in parsed.items():
            if not job_dict.get(key):
                job_dict[key] = val
        if not job_dict.get("description"):
            job_dict["description"] = raw_text.strip()
    return job_dict


# ── match: job -> ranked candidates ─────────────────────────────────────────

async def match_job(
    job: JobInput | None, raw_text: str | None = None, *, top_k: int | None = None, pool: int | None = None
) -> list[Candidate]:
    settings = get_settings()
    top_k = top_k or settings.rerank_top
    pool = pool or settings.candidate_pool

    job_dict = await _resolve_job(job, raw_text)
    job_text = tb.build_job_text(job_dict)
    job_summary = tb.build_job_summary(job_dict)
    job_skill_list = tb.job_skills(job_dict)

    # 1. Shortlist by semantic similarity.
    job_vec = await embedding.embed_text(job_text)
    hits = await get_store().query(job_vec, pool)
    if not hits:
        return []

    # 2. LLM re-rank the shortlist.
    cand_payload = [
        {"id": h.resume_id, "name": h.candidate_name, "summary": h.summary, "skills": h.skills}
        for h in hits
    ]
    verdicts = await llm.rerank(job_summary, job_skill_list, cand_payload)

    # 3. Blend similarity + LLM judgment, then rank.
    candidates: list[Candidate] = []
    for h in hits:
        v = verdicts.get(h.resume_id)
        if v is None:
            # LLM dropped this id — fall back to similarity only.
            fit = int(round(h.similarity * 100))
            candidates.append(
                Candidate(
                    resume_id=h.resume_id,
                    candidate_name=h.candidate_name,
                    fit_score=fit,
                    similarity=round(h.similarity, 4),
                    qualified=fit >= 60,
                    verdict="possible" if fit >= 60 else "weak",
                    matched_skills=[],
                    missing_skills=[],
                    rationale=None,
                )
            )
            continue
        blended = settings.sim_weight * (h.similarity * 100) + settings.llm_weight * v["fit_score"]
        candidates.append(
            Candidate(
                resume_id=h.resume_id,
                candidate_name=h.candidate_name,
                fit_score=int(round(max(0, min(100, blended)))),
                similarity=round(h.similarity, 4),
                qualified=v["qualified"],
                verdict=v["verdict"],
                matched_skills=v["matched_skills"],
                missing_skills=v["missing_skills"],
                rationale=v["rationale"],
            )
        )

    candidates.sort(key=lambda c: c.fit_score, reverse=True)
    return candidates[:top_k]


# ── score: one resume vs a job ──────────────────────────────────────────────

async def score_one(req: ScoreRequest) -> ScoreResponse:
    job_dict = await _resolve_job(req.job, req.job_text)
    job_summary = tb.build_job_summary(job_dict)
    job_skill_list = tb.job_skills(job_dict)

    resume_summary: str
    resume_skills: list[str]
    candidate_name = req.candidate_name
    resume_id = req.resume_id

    if req.analysis is not None:
        analysis = req.analysis.model_dump()
        resume_summary = tb.build_resume_summary(analysis, candidate_name)
        resume_skills = tb.resume_skills(analysis)
    elif req.text and req.text.strip():
        resume_summary = req.text.strip()[:1500]
        resume_skills = []
    elif req.resume_id:
        stored = await get_store().get(req.resume_id)
        if stored is None:
            raise LookupError(f"Resume '{req.resume_id}' not found in the store.")
        resume_summary = stored.summary
        resume_skills = stored.skills
        candidate_name = candidate_name or stored.candidate_name
    else:  # guarded in the route, but be explicit
        raise ValueError("Provide resume_id, analysis, or text.")

    verdict = await llm.score_one(job_summary, job_skill_list, resume_summary, resume_skills)
    return ScoreResponse(
        resume_id=resume_id,
        candidate_name=candidate_name,
        fit_score=verdict["fit_score"],
        qualified=verdict["qualified"],
        verdict=verdict["verdict"],
        matched_skills=verdict["matched_skills"],
        missing_skills=verdict["missing_skills"],
        rationale=verdict["rationale"],
    )
