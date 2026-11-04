"""A generation claim released automatically when a worker exits."""
from contextlib import contextmanager
from sqlalchemy import text
from api_module.database import engine

@contextmanager
def match_report_lease(favorite_id, user_id):
    with engine.begin() as connection:
        acquired = connection.execute(text('SELECT pg_try_advisory_xact_lock(hashtextextended(:key,0))'),
            {'key': f'enterprise-match-report:{user_id}:{favorite_id}'}).scalar_one()
        yield bool(acquired)
