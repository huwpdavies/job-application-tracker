"""User-editable settings: stored in the DB, defaults come from .env."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import db
from .config import Config
from .prefilter import DEFAULT_KEYWORDS, DEFAULT_SENDER_DOMAINS


@dataclass
class Settings:
    confidence_threshold: float
    follow_up_days: int
    claude_model: str
    applications_folder: str
    inbox_check: bool
    calendar_check: bool
    sender_domains: list[str]
    keywords: list[str]


def defaults(cfg: Config) -> dict:
    return {
        "confidence_threshold": cfg.confidence_threshold,
        "follow_up_days": cfg.follow_up_days,
        "claude_model": cfg.claude_model,
        "applications_folder": cfg.applications_folder,
        "inbox_check": True,
        "calendar_check": True,
        "sender_domains": DEFAULT_SENDER_DOMAINS,
        "keywords": DEFAULT_KEYWORDS,
    }


def load(conn: sqlite3.Connection, cfg: Config) -> Settings:
    vals = defaults(cfg)
    for row in conn.execute("SELECT key, value FROM settings"):
        if row["key"] in vals:
            vals[row["key"]] = db.jloads(row["value"], vals[row["key"]])
    return Settings(**vals)


def save(conn: sqlite3.Connection, key: str, value) -> None:
    db.kv_set(conn, "settings", key, json.dumps(value))
