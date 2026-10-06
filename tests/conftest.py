import pytest

from tracker import db
from tracker.config import Config


@pytest.fixture
def cfg(tmp_path):
    """A config that points everything (DB, token cache, classification cache) at a temp folder."""
    return Config(
        client_id="test-client", anthropic_api_key="sk-test", db_path=tmp_path / "tracker.db", claude_model="claude-haiku-4-5",
        confidence_threshold=0.7, follow_up_days=14, applications_folder="Job Applications", host="127.0.0.1", port=8000,
        token_cache_path=tmp_path / "token_cache.json", machine_name="TESTBOX",
    )


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.db_path)
    yield c
    c.close()
