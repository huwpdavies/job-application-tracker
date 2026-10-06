"""Cheap pre-filter, used where emails are NOT already in the Job Applications folder.

Used for the Inbox check (together with known linked sender domains) and for spotting
interview-like calendar events. Both lists are editable in Settings.
"""
from __future__ import annotations

import re

DEFAULT_SENDER_DOMAINS = [
    # Job boards
    "linkedin.com", "indeed.com", "indeedemail.com", "reed.co.uk", "totaljobs.com", "cv-library.co.uk",
    "glassdoor.com", "monster.co.uk", "adzuna.co.uk", "jobsite.co.uk", "ziprecruiter.com", "otta.com",
    "welcometothejungle.com", "wellfound.com", "workingnomads.com",
    # Applicant tracking systems
    "myworkday.com", "myworkdayjobs.com", "workday.com", "greenhouse.io", "greenhouse-mail.io", "lever.co",
    "hire.lever.co", "smartrecruiters.com", "ashbyhq.com", "workable.com", "icims.com", "successfactors.com",
    "successfactors.eu", "taleo.net", "oraclecloud.com", "jobvite.com", "teamtailor.com", "bamboohr.com",
    "recruitee.com", "pinpointhq.com", "eightfold.ai", "hirevue.com", "beamery.com", "cezanne-hr.com",
]

DEFAULT_KEYWORDS = [
    "your application", "application received", "application submitted", "thank you for applying",
    "thanks for applying", "we received your application", "application status", "application update",
    "unfortunately", "not been successful", "not progress", "will not be moving forward",
    "interview", "phone screen", "screening call", "assessment", "coding test", "online test",
    "video interview", "invite you to", "next steps", "offer of employment", "job offer", "recruiter",
    "candidate", "vacancy", "your cv", "shortlisted",
]

INTERVIEW_EVENT_KEYWORDS = [
    "interview", "screening", "phone screen", "intro call", "introductory call", "meet the team",
    "technical round", "final round", "assessment", "hiring manager", "recruiter call", "talent",
    "candidate", "teams meeting with", "chat with",
]


def domain_of(address: str | None) -> str:
    return (address or "").rsplit("@", 1)[-1].strip().lower()


def domain_matches(domain: str, domains: list[str]) -> bool:
    """True if `domain` equals or is a subdomain of any entry (mail.greenhouse.io matches greenhouse.io)."""
    d = domain.lower()
    return any(d == x or d.endswith("." + x) for x in (s.lower().lstrip("@") for s in domains))


def keyword_hit(text: str, keywords: list[str]) -> bool:
    t = text.lower()
    return any(re.search(r"\b" + re.escape(k.lower()), t) for k in keywords)


def looks_job_related(
    sender_domain: str,
    subject: str,
    snippet: str = "",
    sender_domains: list[str] | None = None,
    keywords: list[str] | None = None,
    known_domains: list[str] | None = None,
) -> bool:
    """Inbox pre-filter: known applicant domain, or a job board/ATS sender, or a keyword in subject/snippet."""
    if known_domains and domain_matches(sender_domain, known_domains):
        return True
    if domain_matches(sender_domain, sender_domains or DEFAULT_SENDER_DOMAINS):
        return True
    return keyword_hit(f"{subject} {snippet}", keywords or DEFAULT_KEYWORDS)


def looks_like_interview_event(subject: str, body_preview: str = "", keywords: list[str] | None = None) -> bool:
    return keyword_hit(f"{subject} {body_preview}", keywords or INTERVIEW_EVENT_KEYWORDS)
