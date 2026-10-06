from datetime import datetime, timedelta, timezone

import fixtures as fx
from tracker import lock
from tracker.textclean import MAX_CHARS, clean_body, html_to_text


def test_html_becomes_plain_text_without_scripts_or_styles():
    html = "<html><head><style>p{color:red}</style></head><body><script>alert(1)</script><p>Hello</p><p>World</p></body></html>"
    text = clean_body(html)
    assert "Hello" in text and "World" in text and "alert" not in text and "color" not in text


def test_links_are_shortened_to_their_host():
    text = clean_body("<p>Start here: https://app.hirevue.com/candidates/assess/8f3a?token=SECRET123 now</p>")
    assert "SECRET123" not in text and "[link: app.hirevue.com]" in text


def test_footer_after_the_message_is_stripped():
    text = clean_body(fx.LINKEDIN_CONFIRMATION["html"])
    assert "Northwind Analytics" in text and "Senior Data Analyst" in text
    assert "Sunnyvale" not in text and "Unsubscribe" not in text


def test_footer_marker_inside_the_first_lines_is_not_cut():
    # a short email that merely starts with a marker word must keep its content
    assert "Privacy" in clean_body("<p>Privacy policy update for your application</p>")


def test_long_bodies_are_truncated_and_marked():
    text = clean_body("<p>" + ("word " * 5000) + "</p>")
    assert len(text) <= MAX_CHARS + 20 and text.endswith("[truncated]")


def test_plain_text_bodies_work_and_whitespace_is_collapsed():
    text = clean_body("Line one   \r\n\r\n\r\n\r\nLine   two", "text")
    assert text == "Line one\n\nLine two"


def test_invisible_characters_removed():
    assert clean_body("<p>Hel​lo­</p>") == "Hello"


def test_html_to_text_handles_empty():
    assert html_to_text("").strip() == ""


# ---- lock file ----
def test_no_warning_when_no_lock(tmp_path):
    assert lock.foreign_lock(tmp_path / "x.lock", "A") is None


def test_warning_for_other_machine_under_two_hours(tmp_path):
    p = tmp_path / "x.lock"
    lock.write_lock(p, "OTHER")
    assert lock.foreign_lock(p, "ME")["machine"] == "OTHER"


def test_no_warning_for_own_machine(tmp_path):
    p = tmp_path / "x.lock"
    lock.write_lock(p, "ME")
    assert lock.foreign_lock(p, "ME") is None


def test_no_warning_once_lock_is_two_hours_old(tmp_path):
    p = tmp_path / "x.lock"
    lock.write_lock(p, "OTHER")
    later = datetime.now(timezone.utc) + timedelta(hours=2, minutes=1)
    assert lock.foreign_lock(p, "ME", now=later) is None
    just_under = datetime.now(timezone.utc) + timedelta(hours=1, minutes=59)
    assert lock.foreign_lock(p, "ME", now=just_under) is not None


def test_corrupt_lock_is_ignored(tmp_path):
    p = tmp_path / "x.lock"
    p.write_text("not json")
    assert lock.foreign_lock(p, "ME") is None


def test_release_only_removes_own_lock(tmp_path):
    p = tmp_path / "x.lock"
    lock.write_lock(p, "OTHER")
    lock.release_lock(p, "ME")
    assert p.exists()
    lock.release_lock(p, "OTHER")
    assert not p.exists()
