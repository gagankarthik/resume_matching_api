"""
Project a structured resume or a job into (a) a compact text string used for
embedding, (b) a short human-readable summary shown to the LLM re-ranker, and
(c) a flat list of skills used for quick matched/missing display.

We embed a *matching-relevant projection*, not the raw resume — summary, level,
industry, years, skills, job titles, technologies, degrees, certs — so a resume
vector and a job vector live in the same semantic space.
"""
from __future__ import annotations

import re
from typing import Any

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(value: Any) -> str:
    """Coerce to a plain, whitespace-collapsed string; strip HTML tags."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(_clean(v) for v in value if v)
    text = str(value)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _as_lines(value: str | list[str] | None) -> list[str]:
    """requirements/responsibilities may be a list, an HTML/rich-text string, or
    plain text. Normalise to a list of clean lines."""
    if value is None:
        return []
    if isinstance(value, list):
        return [_clean(v) for v in value if _clean(v)]
    # A string — split <li>/<br>/newlines into lines.
    raw = str(value)
    raw = re.sub(r"</li>|<br\s*/?>|</p>", "\n", raw, flags=re.IGNORECASE)
    parts = [_clean(p) for p in raw.split("\n")]
    return [p for p in parts if p]


def _dedupe(items: list[str], limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = it.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it.strip())
        if limit and len(out) >= limit:
            break
    return out


# ── Skills extraction ───────────────────────────────────────────────────────

_SKILL_BUCKETS = [
    "technical_skills",
    "programming_languages",
    "frameworks_and_libraries",
    "databases",
    "cloud_platforms",
    "tools_and_platforms",
    "methodologies",
    "domain_skills",
    "design_skills",
    "soft_skills",
    "other_skills",
    "all_skills_raw",
]


def resume_skills(analysis: dict[str, Any]) -> list[str]:
    """Flatten every skill bucket of a ResumeAnalysis into one deduped list."""
    skills_obj = analysis.get("skills") or {}
    collected: list[str] = []
    for bucket in _SKILL_BUCKETS:
        vals = skills_obj.get(bucket)
        if isinstance(vals, list):
            collected.extend(str(v) for v in vals if v)
    # categories: [{name, skills: [...]}]
    for cat in skills_obj.get("categories") or []:
        if isinstance(cat, dict):
            collected.extend(str(v) for v in (cat.get("skills") or []) if v)
    # Fall back to technologies used across work + projects.
    for exp in analysis.get("work_experience") or []:
        if isinstance(exp, dict):
            collected.extend(str(v) for v in (exp.get("technologies_used") or []) if v)
    for proj in analysis.get("projects") or []:
        if isinstance(proj, dict):
            collected.extend(str(v) for v in (proj.get("technologies") or []) if v)
    return _dedupe(collected)


# ── Resume → text / summary ─────────────────────────────────────────────────

def build_resume_text(analysis: dict[str, Any], candidate_name: str | None = None) -> str:
    """The string that gets embedded for a resume."""
    a = analysis
    analytics = a.get("analytics") or {}
    parts: list[str] = []

    if candidate_name:
        parts.append(f"Candidate: {candidate_name}")

    summary = _clean(a.get("professional_summary") or a.get("objective"))
    if summary:
        parts.append(f"Summary: {summary}")

    level = _clean(analytics.get("career_level"))
    if level:
        parts.append(f"Career level: {level}")

    industry = _clean(analytics.get("primary_industry"))
    if industry:
        parts.append(f"Industry: {industry}")

    functions = analytics.get("job_functions")
    if functions:
        parts.append(f"Functions: {_clean(functions)}")

    years = analytics.get("total_years_of_experience")
    if years is not None:
        parts.append(f"Years of experience: {years}")

    # Job titles + technologies from work history.
    titles: list[str] = []
    techs: list[str] = []
    for exp in a.get("work_experience") or []:
        if not isinstance(exp, dict):
            continue
        t = _clean(exp.get("job_title"))
        if t:
            titles.append(t)
        techs.extend(str(v) for v in (exp.get("technologies_used") or []) if v)
    if titles:
        parts.append(f"Roles: {', '.join(_dedupe(titles, 12))}")

    # Education.
    degrees: list[str] = []
    for edu in a.get("education") or []:
        if not isinstance(edu, dict):
            continue
        deg = " ".join(x for x in [_clean(edu.get("degree")), _clean(edu.get("field_of_study"))] if x)
        if deg:
            degrees.append(deg)
    if degrees:
        parts.append(f"Education: {', '.join(_dedupe(degrees, 6))}")

    # Certifications.
    certs = [_clean(c.get("name")) for c in (a.get("certifications") or []) if isinstance(c, dict) and _clean(c.get("name"))]
    if certs:
        parts.append(f"Certifications: {', '.join(_dedupe(certs, 10))}")

    skills = resume_skills(a)
    if skills:
        parts.append(f"Skills: {', '.join(skills[:60])}")

    return "\n".join(parts).strip()


def build_resume_summary(analysis: dict[str, Any], candidate_name: str | None = None) -> str:
    """A short summary (fed to the LLM re-ranker so it can reason without the
    full resume). Kept tight to bound re-rank token cost."""
    a = analysis
    analytics = a.get("analytics") or {}
    bits: list[str] = []
    if candidate_name:
        bits.append(candidate_name)
    lvl = _clean(analytics.get("career_level"))
    yrs = analytics.get("total_years_of_experience")
    ind = _clean(analytics.get("primary_industry"))
    head = ", ".join(x for x in [lvl, f"{yrs} yrs" if yrs is not None else "", ind] if x)
    if head:
        bits.append(head)
    summary = _clean(a.get("professional_summary") or a.get("objective"))
    if summary:
        bits.append(summary[:400])
    titles = _dedupe([_clean(e.get("job_title")) for e in (a.get("work_experience") or []) if isinstance(e, dict) and _clean(e.get("job_title"))], 6)
    if titles:
        bits.append("Roles: " + ", ".join(titles))
    skills = resume_skills(a)
    if skills:
        bits.append("Skills: " + ", ".join(skills[:40]))
    return " | ".join(bits)[:1200]


# ── Job → text / summary / skills ───────────────────────────────────────────

def build_job_text(job: dict[str, Any]) -> str:
    parts: list[str] = []
    if _clean(job.get("title")):
        parts.append(f"Job title: {_clean(job.get('title'))}")
    for key in ("seniority", "employment_type", "location"):
        if _clean(job.get(key)):
            parts.append(f"{key.replace('_', ' ').title()}: {_clean(job.get(key))}")
    if _clean(job.get("description")):
        parts.append(f"Description: {_clean(job.get('description'))}")
    reqs = _as_lines(job.get("requirements"))
    if reqs:
        parts.append("Requirements: " + "; ".join(reqs))
    resp = _as_lines(job.get("responsibilities"))
    if resp:
        parts.append("Responsibilities: " + "; ".join(resp))
    skills = job_skills(job)
    if skills:
        parts.append("Required skills: " + ", ".join(skills))
    return "\n".join(parts).strip()


def build_job_summary(job: dict[str, Any]) -> str:
    parts: list[str] = []
    if _clean(job.get("title")):
        parts.append(f"Title: {_clean(job.get('title'))}")
    if _clean(job.get("seniority")):
        parts.append(_clean(job.get("seniority")))
    desc = _clean(job.get("description"))
    if desc:
        parts.append(desc[:500])
    reqs = _as_lines(job.get("requirements"))
    if reqs:
        parts.append("Requirements: " + "; ".join(reqs[:12]))
    skills = job_skills(job)
    if skills:
        parts.append("Required skills: " + ", ".join(skills))
    return " | ".join(parts)[:1500]


def job_skills(job: dict[str, Any]) -> list[str]:
    skills: list[str] = []
    if isinstance(job.get("skills"), list):
        skills.extend(str(s) for s in job["skills"] if s)
    return _dedupe(skills)
