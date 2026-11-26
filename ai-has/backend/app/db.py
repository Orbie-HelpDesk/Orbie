import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from .config import settings

# On Vercel every request can start its own process, so a connection pool per process quickly
# uses up the database's connection limit (EMAXCONNSESSION on Supabase). There, open a connection
# when it is needed and close it right after. Locally (Docker) the normal pool is kept.
if os.environ.get("VERCEL"):
    engine = create_engine(settings.database_url, poolclass=NullPool, pool_pre_ping=True)
else:
    engine = create_engine(settings.database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()