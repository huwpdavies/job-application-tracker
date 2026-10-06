"""Turn email bodies into short plain text suitable for classification."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

MAX_CHARS = 4000

_URL = re.compile(r"https?://[^\s<>\")\]]+")
_INVISIBLE = re.compile("[​‌‍⁠﻿­͏]")
_BLANKS = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t ]+")

# A line starting with one of these (after the first part of the message) begins the footer/signature.
_FOOTER_MARKERS = re.compile(
    r"^\s*(?:--+\s*$|__+\s*$|unsubscribe\b|to unsubscribe|you are receiving this|you received this|"
    r"this (?:e-?mail|message) (?:was sent|is intended|and any)|view (?:this email )?in (?:your )?browser|"
    r"manage (?:your )?(?:email )?(?:preferences|notifications|alerts)|privacy (?:policy|notice)|"
    r"sent from my |get outlook for|confidentiality notice|disclaimer\b|©|copyright\b|"
    r"linkedin corporation|indeed,? inc|if you (?:no longer|don't|do not) want to receive)",
    re.IGNORECASE,
)
_MIN_KEEP = 120  # never cut before this many characters


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "head", "title", "meta", "noscript"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"]):
        block.append("\n")
    return soup.get_text()


def _shorten_urls(text: str) -> str:
    def repl(m: re.Match) -> str:
        host = urlparse(m.group(0)).netloc.lower().removeprefix("www.")
        return f"[link: {host}]" if host else "[link]"

    return _URL.sub(repl, text)


def strip_footer(text: str) -> str:
    pos = 0
    for line in text.splitlines(keepends=True):
        if pos >= _MIN_KEEP and _FOOTER_MARKERS.match(line):
            return text[:pos].rstrip()
        pos += len(line)
    return text


def clean_body(body: str, content_type: str = "html", max_chars: int = MAX_CHARS) -> str:
    text = html_to_text(body) if content_type.lower() == "html" else (body or "")
    text = _INVISIBLE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    text = _shorten_urls(text)
    text = "\n".join(_SPACES.sub(" ", ln).strip() for ln in text.split("\n"))
    text = _BLANKS.sub("\n\n", text).strip()
    text = strip_footer(text)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[truncated]"
    return text
