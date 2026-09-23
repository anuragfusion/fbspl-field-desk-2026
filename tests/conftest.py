"""Loaded by pytest before any test module. app/db.py calls load_dotenv(), and
.env holds PRODUCTION credentials, so tests must pin every external target
before anything imports the app — and refuse to run if pinned anywhere remote."""

import os
from urllib.parse import urlparse

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/postgres")
# Empty (not unset) so load_dotenv() cannot fill them in from .env.
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_SERVICE_ROLE_KEY"] = ""

_host = urlparse(os.environ["DATABASE_URL"]).hostname
if _host not in ("localhost", "127.0.0.1", "::1"):
    raise SystemExit(f"Refusing to run tests against non-local database host {_host!r}.")
