"""Database engine and session factory."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from uptime.config import get_settings

# pool_pre_ping checks a pooled connection before using it, so a database
# restart doesn't hand out dead connections. pool_size/max_overflow cap how
# many connections each process can open (protects max_connections).
engine = create_engine(
    get_settings().database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
