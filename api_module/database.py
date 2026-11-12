import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

load_dotenv()

USER = os.getenv("user")
PASSWORD = os.getenv("password")
HOST = os.getenv("host")
PORT = os.getenv("port")
DBNAME = os.getenv("dbname")

if not (USER and PASSWORD and HOST and PORT and DBNAME):
    raise RuntimeError(
        f"DB env missing. Got user={USER!r} host={HOST!r} port={PORT!r} db={DBNAME!r}"
    )

DATABASE_URL = (
    f"postgresql+psycopg2://{USER}:{PASSWORD}@{HOST}:{PORT}/{DBNAME}?sslmode=require"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
# Keep benchmark cache transactions independent of the main request pool.
# Four connections maximum: callers cannot exhaust their own pool while waiting
# for a second connection to finish a cache refresh/ranking transaction.
benchmark_engine = create_engine(
    DATABASE_URL, pool_pre_ping=True, pool_size=2, max_overflow=2, pool_timeout=20,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
