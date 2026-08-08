import os
import sys
from pathlib import Path

import pytest

# evals/ sits next to otto/, not inside it — make both importable when
# running `pytest evals` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(scope="session", autouse=True)
def cleanup_postgres_test_schemas():
    """Drop only this pytest process's isolated schemas after the suite."""
    yield
    try:
        import psycopg
        from psycopg import sql

        url = os.getenv("OTTO_DATABASE_URL", "postgresql:///otto")
        prefix = f"otto_test_{os.getpid()}_"
        conn = psycopg.connect(url, autocommit=True)
        rows = conn.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE %s",
            (prefix + "%",),
        ).fetchall()
        for (name,) in rows:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))
        conn.close()
    except Exception:
        # Test failures must report the product failure, not mask it with cleanup.
        pass
