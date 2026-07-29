"""
LLM re-ranking and single-resume scoring with gpt-4.1-mini.

The embedding stage produces a shortlist; this stage turns a shortlist (or one
resume) into a precise, explained verdict. Uses chat completions with JSON mode,
mirroring the extraction engine's OpenAI usage.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from config import get_settings
from embedding import _get_client  # reuse the singleton AsyncOpenAI (one pool)

logger = logging.getLogger("matching.llm")

_RETRY_HINT_RE = re.compile(r"try again in (\d+(?:\.\d+)?)\s*(ms|s|second|seconds)", re.IGNORECASE)

VALID_VERDICTS = {"strong", "possible", "weak"}


def _parse_retry_after(exc: Exception) -> float | None:
    m = _RETRY_HINT_RE.search(str(exc))
    if not m:
        return None
    value = float(m.group(1))
    return value / 1000.0 if m.group(2).lower() == "ms" else value


def _parse_json(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        end = len(lines) - 1 if lines and lines[-1].strip() == "```" else len(lines)
        text = "\n".join(lines[1:end])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if m:
            return json.loads(m.group(1))
        raise


async def _chat_json(system: str, user: str, *, retries: int = 4, max_tokens: int = 4096) -> Any:
    settings = get_settings()
    client = _get_client()
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.chat.completions.create(
                model=settings.chat_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            return _parse_json(resp.choices[0].message.content or "")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries:
                hinted = _parse_retry_after(exc)
                wait = (hinted + 0.1 * (attempt + 1)) if hinted is not None else min(30.0, 2 ** attempt)
                logger.warning("chat attempt %d failed: %s — retrying in %.2fs", attempt + 1, exc, wait)
                await asyncio.sleep(wait)
    raise RuntimeError(f"All chat attempts failed: {last_exc}") from last_exc


def _norm_verdict(v: Any) -> str:
    s = str(v or "").strip().lower()
    return s if s in VALID_VERDICTS else ("strong" if s in {"excellent", "great"} else "weak")


def _clamp_score(v: Any) -> int:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


def _str_list(v: Any, limit: int = 25) -> list[str]:
    if not isinstance(v, list):
        return []
    out = [str(x).strip() for x in v if x is not None and str(x).strip()]
    return out[:limit]


_RERANK_SYSTEM = (
    "You are an expert technical recruiter. You are given ONE job and a list of "
    "candidate resume summaries. For EACH candidate, judge how well they fit the "
    "job. Be strict and evidence-based: only credit skills/experience actually "
    "present in the candidate summary. Return STRICT JSON only."
)


async def rerank(job_summary: str, job_skill_list: list[str], candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Score every shortlisted candidate against the job.

    `candidates`: [{ "id", "name", "summary", "skills": [...] }]
    Returns: { id -> { fit_score, qualified, verdict, matched_skills, missing_skills, rationale } }
    """
    if not candidates:
        return {}

    job_block = job_summary
    if job_skill_list:
        job_block += "\nRequired skills: " + ", ".join(job_skill_list)

    cand_lines = []
    for c in candidates:
        skills = ", ".join(c.get("skills") or [])
        cand_lines.append(
            f'- id: "{c["id"]}"\n  name: {c.get("name") or "Unknown"}\n  summary: {c.get("summary") or ""}\n  skills: {skills}'
        )
    candidates_block = "\n".join(cand_lines)

    user = (
        f"=== JOB ===\n{job_block}\n\n"
        f"=== CANDIDATES ({len(candidates)}) ===\n{candidates_block}\n\n"
        "For each candidate return an object with:\n"
        '  "id": the exact candidate id,\n'
        '  "fit_score": integer 0-100 (how well they match the job),\n'
        '  "qualified": boolean (true only if they clearly meet the core requirements),\n'
        '  "verdict": one of "strong" | "possible" | "weak",\n'
        '  "matched_skills": job-relevant skills the candidate HAS (max ~10),\n'
        '  "missing_skills": important job skills the candidate LACKS (max ~10),\n'
        '  "rationale": one concise sentence.\n'
        'Respond as JSON: {"results": [ ... ]} — include EVERY candidate id exactly once.'
    )

    data = await _chat_json(_RERANK_SYSTEM, user, max_tokens=4096)
    results = data.get("results") if isinstance(data, dict) else data
    out: dict[str, dict[str, Any]] = {}
    for r in results or []:
        if not isinstance(r, dict) or "id" not in r:
            continue
        out[str(r["id"])] = {
            "fit_score": _clamp_score(r.get("fit_score")),
            "qualified": bool(r.get("qualified")),
            "verdict": _norm_verdict(r.get("verdict")),
            "matched_skills": _str_list(r.get("matched_skills")),
            "missing_skills": _str_list(r.get("missing_skills")),
            "rationale": (str(r.get("rationale")).strip() or None) if r.get("rationale") else None,
        }
    return out


_JOB_PARSE_SYSTEM = (
    "You extract a structured job posting from raw pasted text (which may be messy "
    "copy-paste from a job board or email). Return STRICT JSON only. Do not invent "
    "requirements that aren't in the text."
)


async def parse_job(text: str) -> dict[str, Any]:
    """Turn a raw pasted job description into structured fields so it matches as
    well as an integration payload does."""
    user = (
        f"=== JOB POSTING ===\n{text[:8000]}\n\n"
        "Return JSON with:\n"
        '  "title": the role title (string),\n'
        '  "seniority": e.g. "Junior"|"Mid"|"Senior"|"Lead"|null,\n'
        '  "employment_type": e.g. "Full-time"|"Contract"|null,\n'
        '  "location": string|null,\n'
        '  "skills": required/preferred skills as a list of short strings,\n'
        '  "requirements": list of requirement lines,\n'
        '  "responsibilities": list of responsibility lines.\n'
        "Use null/empty when the text doesn't say."
    )
    data = await _chat_json(_JOB_PARSE_SYSTEM, user, max_tokens=1500)
    if not isinstance(data, dict):
        data = {}
    return {
        "title": (str(data["title"]).strip() if data.get("title") else None),
        "seniority": (str(data["seniority"]).strip() if data.get("seniority") else None),
        "employment_type": (str(data["employment_type"]).strip() if data.get("employment_type") else None),
        "location": (str(data["location"]).strip() if data.get("location") else None),
        "skills": _str_list(data.get("skills"), 40),
        "requirements": _str_list(data.get("requirements"), 40),
        "responsibilities": _str_list(data.get("responsibilities"), 40),
    }


_SCORE_SYSTEM = (
    "You are an expert technical recruiter. Judge whether ONE candidate qualifies "
    "for ONE job. Be strict and evidence-based: only credit skills/experience "
    "actually present in the resume. Return STRICT JSON only."
)


async def score_one(job_summary: str, job_skill_list: list[str], resume_summary: str, resume_skill_list: list[str]) -> dict[str, Any]:
    job_block = job_summary
    if job_skill_list:
        job_block += "\nRequired skills: " + ", ".join(job_skill_list)
    resume_block = resume_summary
    if resume_skill_list:
        resume_block += "\nSkills: " + ", ".join(resume_skill_list)

    user = (
        f"=== JOB ===\n{job_block}\n\n"
        f"=== CANDIDATE RESUME ===\n{resume_block}\n\n"
        "Return JSON with:\n"
        '  "fit_score": integer 0-100,\n'
        '  "qualified": boolean,\n'
        '  "verdict": "strong" | "possible" | "weak",\n'
        '  "matched_skills": [...], "missing_skills": [...],\n'
        '  "rationale": one or two concise sentences explaining the decision.'
    )

    r = await _chat_json(_SCORE_SYSTEM, user, max_tokens=1024)
    if not isinstance(r, dict):
        r = {}
    return {
        "fit_score": _clamp_score(r.get("fit_score")),
        "qualified": bool(r.get("qualified")),
        "verdict": _norm_verdict(r.get("verdict")),
        "matched_skills": _str_list(r.get("matched_skills")),
        "missing_skills": _str_list(r.get("missing_skills")),
        "rationale": (str(r.get("rationale")).strip() or None) if r.get("rationale") else None,
    }
