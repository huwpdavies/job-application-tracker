"""Application status rules. Automatic changes only ever move forward."""
from __future__ import annotations

STATUSES = ["Applied", "Interviewing", "Offer", "Rejected", "Withdrawn", "No response"]

# Higher rank = further along. Withdrawn is manual-only (never changed automatically).
RANK = {"No response": 1, "Applied": 1, "Interviewing": 2, "Offer": 3, "Rejected": 3, "Withdrawn": 99}

# Status an email category implies (None = no status change).
CATEGORY_STATUS = {
    "application_confirmation": "Applied",
    "acknowledgement": "Applied",
    "interview_invite": "Interviewing",
    "interview_scheduled": "Interviewing",
    "interview_rescheduled": "Interviewing",
    "assessment": "Interviewing",  # a test or video-question stage means you are past "Applied"
    "rejection": "Rejected",
    "offer": "Offer",
    # recruiter_message, interview_cancelled, other: no change
}

INTERVIEW_CATEGORIES = {
    "interview_invite", "interview_scheduled", "interview_rescheduled", "interview_cancelled", "assessment",
}
TERMINAL = {"Offer", "Rejected", "Withdrawn"}


def advance(current: str, target: str | None) -> str:
    """Return the status after an automatic update: only moves forward, never resets, never touches Withdrawn."""
    if not target or current == "Withdrawn":
        return current
    return target if RANK[target] > RANK[current] else current
