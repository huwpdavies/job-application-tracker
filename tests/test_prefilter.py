from tracker.prefilter import (DEFAULT_SENDER_DOMAINS, domain_matches, domain_of, keyword_hit, looks_job_related,
                               looks_like_interview_event)


def test_domain_of():
    assert domain_of("Jobs-NoReply@LinkedIn.com") == "linkedin.com"
    assert domain_of(None) == ""
    assert domain_of("not-an-address") == "not-an-address"


def test_domain_matches_subdomains_but_not_lookalikes():
    assert domain_matches("greenhouse.io", DEFAULT_SENDER_DOMAINS)
    assert domain_matches("mail.greenhouse.io", DEFAULT_SENDER_DOMAINS)
    assert domain_matches("acme.myworkdayjobs.com", DEFAULT_SENDER_DOMAINS)
    assert not domain_matches("notgreenhouse.io", DEFAULT_SENDER_DOMAINS)
    assert not domain_matches("greenhouse.io.evil.com", DEFAULT_SENDER_DOMAINS)


def test_keyword_hit_is_case_insensitive_and_word_anchored():
    assert keyword_hit("Your Application Update", ["application update"])
    assert keyword_hit("We'd like to arrange Interviews", ["interview"])      # prefix of a word is fine
    assert not keyword_hit("Reinterview season", ["interview"])               # but not the middle of one
    assert not keyword_hit("Weekend plans", ["interview", "offer of employment"])


def test_known_application_sender_always_passes():
    assert looks_job_related("acme-careers.com", "Hello", known_domains=["acme-careers.com"])


def test_job_board_sender_passes_without_keyword():
    assert looks_job_related("linkedin.com", "Something")


def test_unknown_sender_needs_a_keyword():
    assert not looks_job_related("randomshop.com", "50% off sale")
    assert looks_job_related("randomshop.com", "Thank you for applying to our vacancy")
    assert looks_job_related("randomshop.com", "Hi", snippet="we would like to invite you to interview")


def test_custom_lists_replace_defaults():
    assert not looks_job_related("linkedin.com", "x", sender_domains=["other.com"], keywords=["zzz"])
    assert looks_job_related("other.com", "x", sender_domains=["other.com"], keywords=["zzz"])
    assert looks_job_related("randomshop.com", "contains zzz", sender_domains=["other.com"], keywords=["zzz"])


def test_calendar_event_interview_detection():
    assert looks_like_interview_event("Interview with Optimove")
    assert looks_like_interview_event("Screening call Huw/Lucie")
    assert looks_like_interview_event("Catch up", "hiring manager chat")
    assert not looks_like_interview_event("Dentist", "6-month check-up")
    assert not looks_like_interview_event("Team lunch")
