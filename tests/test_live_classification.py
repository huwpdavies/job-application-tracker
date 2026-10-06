"""Optional: checks the REAL model agrees with the fixture expectations. Costs about 2 cents.

    RUN_LIVE=1 python -m pytest tests/test_live_classification.py

Skipped by default so the normal test run is free, offline and deterministic.
"""
import os

import pytest

import fixtures as fx
from tracker.classify import Classifier
from tracker.config import load_config
from tracker.textclean import clean_body
from tracker.timeutil import to_utc_iso

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1", reason="set RUN_LIVE=1 to call the real API")]


@pytest.fixture(scope="module")
def classifier():
    cfg = load_config()
    return Classifier(cfg.anthropic_api_key, cfg.claude_model)


@pytest.mark.parametrize("fix", fx.ALL, ids=[f["subject"][:40] for f in fx.ALL])
def test_real_model_matches_expectations(classifier, fix):
    r = classifier.classify(fix["sender"], fix["subject"], to_utc_iso(fix["received"]), clean_body(fix["html"]))
    assert r.error is None, r.error
    c, exp = r.classification, fix["expect"]
    assert c.category == exp["category"]
    if "company" in exp:
        assert exp["company"].lower().split()[0] in (c.company or "").lower()
    if "role_contains" in exp:
        assert exp["role_contains"].lower() in (c.role_title or "").lower()
    if "job_reference" in exp:
        assert c.job_reference == exp["job_reference"]
    if "interview_utc" in exp:
        assert c.interview_datetime == exp["interview_utc"]
    if "format" in exp:
        assert c.interview_format == exp["format"]
    if "is_job_related" in exp:
        assert c.is_job_related is exp["is_job_related"]
    assert c.confidence >= 0.7
