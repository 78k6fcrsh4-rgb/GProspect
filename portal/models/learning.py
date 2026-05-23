"""
portal/models/learning.py
--------------------------
LearningEntry database model — placeholder until full implementation.
"""
from database.db import Base
from sqlalchemy import Column, Integer, String, DateTime, Text
from datetime import datetime, timezone


def _utcnow() -> datetime:
    """Timezone-aware UTC now — drop-in for the deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc)


class LearningEntry(Base):
    __tablename__ = "learning_entries"
    id            = Column(Integer, primary_key=True, index=True)
    org_name      = Column(String, index=True, nullable=False)
    entry_type    = Column(String, nullable=False)
    description   = Column(Text, nullable=True)
    triggered_by  = Column(String, nullable=True)
    created_at    = Column(DateTime(timezone=True), default=_utcnow)