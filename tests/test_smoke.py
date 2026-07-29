"""
Offline smoke tests — no network, no AWS. Exercise the pure logic:
projection/text building, JSON parsing, and score normalisation.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm  # noqa: E402
import text_builder as tb  # noqa: E402

SAMPLE = {
    "professional_summary": "Senior backend engineer with 8 years building payments systems.",
    "analytics": {
        "career_level": "Senior",
        "primary_industry": "Fintech",
        "total_years_of_experience": 8,
        "job_functions": ["Backend", "Platform"],
    },
    "work_experience": [
        {"job_title": "Senior Software Engineer", "technologies_used": ["Python", "AWS", "PostgreSQL"]},
        {"job_title": "Software Engineer", "technologies_used": ["Django", "Redis"]},
    ],
    "education": [{"degree": "B.Sc.", "field_of_study": "Computer Science"}],
    "certifications": [{"name": "AWS Solutions Architect"}],
    "skills": {
        "programming_languages": ["Python", "Go"],
        "cloud_platforms": ["AWS"],
        "databases": ["PostgreSQL", "Redis"],
    },
}

JOB = {
    "title": "Senior Backend Engineer",
    "description": "Build scalable payment APIs.",
    "requirements": "<ul><li>5+ years Python</li><li>AWS experience</li></ul>",
    "responsibilities": ["Design services", "Mentor engineers"],
    "skills": ["Python", "AWS", "PostgreSQL"],
}


def test_resume_skills_flatten():
    skills = tb.resume_skills(SAMPLE)
    lower = [s.lower() for s in skills]
    assert "python" in lower
    assert "postgresql" in lower
    # deduped
    assert len(skills) == len(set(lower))


def test_build_resume_text_contains_key_fields():
    text = tb.build_resume_text(SAMPLE, "Jane Doe")
    assert "Jane Doe" in text
    assert "Senior" in text
    assert "Python" in text
    assert "Skills:" in text


def test_build_resume_summary_bounded():
    summary = tb.build_resume_summary(SAMPLE, "Jane Doe")
    assert summary
    assert len(summary) <= 1200


def test_job_text_normalises_html_and_lists():
    text = tb.build_job_text(JOB)
    # HTML tags from requirements are stripped
    assert "<li>" not in text
    assert "5+ years Python" in text
    assert "Design services" in text
    assert "Required skills:" in text


def test_llm_normalisers():
    assert llm._clamp_score("87") == 87
    assert llm._clamp_score(200) == 100
    assert llm._clamp_score(-5) == 0
    assert llm._clamp_score("nope") == 0
    assert llm._norm_verdict("STRONG") == "strong"
    assert llm._norm_verdict("banana") == "weak"
    assert llm._str_list(["a", "", "b", None]) == ["a", "b"]


def test_llm_parse_json_handles_fences():
    assert llm._parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm._parse_json('prefix {"a": 2} suffix') == {"a": 2}


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
