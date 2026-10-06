import pytest

from helpers import app_row
from tracker.matching import company_match, find_match, norm_company, norm_ref, norm_role, role_score


@pytest.mark.parametrize("raw, expected", [
    ("Acme Ltd", "acme"), ("ACME Limited.", "acme"), ("Acme", "acme"),
    ("Smith & Sons plc", "smith sons"),
    ("Aircall.io, Inc.", "aircall"),
    ("The Foo Group", "foo"),
    ("Northbridge Insurance Group", "northbridge insurance"),
    ("ICS.AI Limited", "ics ai"),
    ("Ltd", "ltd"),                      # never normalise a name down to nothing
    (None, ""),
])
def test_norm_company(raw, expected):
    assert norm_company(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Senior Data Analyst", "senior data analyst"),
    ("Pricing Analyst (R-104522)", "pricing analyst"),
    ("Data Analyst [Remote]", "data analyst"),
    ("Software Engineer (m/f/d)", "software engineer"),
    ("QA / Test  Engineer", "qa test engineer"),
    (None, ""),
])
def test_norm_role(raw, expected):
    assert norm_role(raw) == expected


def test_norm_ref():
    assert norm_ref("R-104 522") == "r104522"
    assert norm_ref(None) == ""


@pytest.mark.parametrize("a, b, expected", [
    ("acme", "acme", True),
    ("northbridge", "northbridge insurance", True),    # one is the leading words of the other
    ("aircall", "aircall", True),
    ("acme", "acne", False),                           # similar but different
    ("harlow pine", "harlow pine consulting", True),
    ("bar", "bar foo", False),                         # too short to trust a prefix match
    ("microsoft", "micro", False),
    ("", "acme", False),
])
def test_company_match(a, b, expected):
    assert company_match(a, b) is expected


@pytest.mark.parametrize("a, b, ok", [
    ("data analyst", "data analyst", True),
    ("cybersecurity engineering internship program", "census cybersecurity engineering internship program learn hack secure", True),
    ("data analyst", "analyst data", True),            # word order
    ("senior data analyst", "data analyst", False),    # seniority differs => different job
    ("junior developer", "lead developer", False),
    ("data analyst", "marketing manager", False),
])
def test_role_score_threshold(a, b, ok):
    assert (role_score(a, b) >= 80) is ok


def test_find_match_same_company_and_role(conn):
    aid = app_row(conn, "Acme Ltd", "Data Analyst")
    m = find_match(conn, "Acme", "Data Analyst", None)
    assert m.app_id == aid and not m.ambiguous


def test_find_match_job_reference_wins(conn):
    a = app_row(conn, "Brightwater Energy", "Reporting Analyst", ref="BE-2291")
    app_row(conn, "Brightwater Energy", "Insight Analyst", ref="BE-9999")
    m = find_match(conn, "Brightwater Energy", "totally different title", "be 2291")
    assert m.app_id == a and m.reason == "job reference"


def test_find_match_same_company_different_role_is_not_a_match(conn):
    app_row(conn, "Acme", "Data Analyst")
    m = find_match(conn, "Acme", "Marketing Manager", None)
    assert m.app_id is None and m.candidates and not m.ambiguous


def test_find_match_application_without_role_takes_first_named_role(conn):
    aid = app_row(conn, "Acme", "")
    assert find_match(conn, "Acme", "Data Analyst", None).app_id == aid


def test_find_match_no_role_single_application(conn):
    aid = app_row(conn, "Acme", "Data Analyst")
    assert find_match(conn, "Acme", None, None).app_id == aid


def test_find_match_no_role_many_applications_prefers_the_only_active_one(conn):
    app_row(conn, "Acme", "Role A", status="Rejected")
    active = app_row(conn, "Acme", "Role B", status="Applied")
    m = find_match(conn, "Acme", None, None)
    assert m.app_id == active


def test_find_match_no_role_many_active_is_ambiguous(conn):
    app_row(conn, "Acme", "Role A")
    app_row(conn, "Acme", "Role B")
    m = find_match(conn, "Acme", None, None)
    assert m.app_id is None and m.ambiguous and len(m.candidates) == 2


def test_find_match_unknown_company(conn):
    app_row(conn, "Acme", "Data Analyst")
    assert find_match(conn, "Globex", "Data Analyst", None).candidates == []
