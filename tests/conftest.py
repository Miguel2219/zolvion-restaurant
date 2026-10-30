"""Every test runs against its own PostgreSQL database, never the app's.

pytest loads this file before any test module imports `app`, and db.DATABASE_URL is
fixed at import. The test database is the app's DATABASE_URL (environment, then .env)
with "_test" added to its name; TEST_DATABASE_URL overrides that. Its name must end
in _test, so the reset before every test can never wipe real data."""

import os
from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict, make_conninfo

# Same default as app/db.py. Importing app here would fix its DATABASE_URL too early.
DEFAULT_URL = "postgresql://zolvion:zolvion@localhost:5432/zolvion"


def _test_url() -> str:
    if os.environ.get("TEST_DATABASE_URL"):
        return os.environ["TEST_DATABASE_URL"]
    env_file = Path(__file__).resolve().parent.parent / ".env"
    base = (os.environ.get("DATABASE_URL") or dotenv_values(env_file).get("DATABASE_URL")
            or DEFAULT_URL)
    return make_conninfo(base, dbname=conninfo_to_dict(base).get("dbname", "zolvion") + "_test")


TEST_URL = _test_url()
NAME = conninfo_to_dict(TEST_URL).get("dbname", "")
if not NAME.endswith("_test"):
    pytest.exit(f"TEST_DATABASE_URL must name a database ending in _test, not {NAME!r}",
                returncode=2)
# Set, not setdefault: a DATABASE_URL in the shell or in .env must not win here.
os.environ["DATABASE_URL"] = TEST_URL
# The tests never reach a real Chatwoot: .env may hold one, and app/__init__.py would load
# it (it never overrides a variable that is already set, even to ""). Chatwoot tests set
# their own values with monkeypatch.
for name in ("CHATWOOT_URL", "CHATWOOT_ACCOUNT_ID", "CHATWOOT_BOT_TOKEN", "CHATWOOT_BOT_SECRET",
             "ELEVENLABS_API_KEY"):    # nor a real transcriber: voice tests set their own
    os.environ[name] = ""

try:
    with psycopg.connect(make_conninfo(TEST_URL, dbname="postgres"), autocommit=True) as server:
        if server.execute("SELECT 1 FROM pg_database WHERE datname=%s", (NAME,)).fetchone() is None:
            server.execute(f'CREATE DATABASE "{NAME}"')
except psycopg.OperationalError as exc:
    pytest.exit(f"No PostgreSQL for the tests ({exc}). See docs/BUILD-AND-RUN.md.", returncode=2)
