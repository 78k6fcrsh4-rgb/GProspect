"""
portal/models/result.py
-----------------------
GrantResult database model — placeholder until full implementation.
"""
from database.db import Base
from sqlalchemy import Column, Integer, String, Float, DateTime, Text
from datetime import datetime

class GrantResult(Base):
    __tablename__ = "grant_results"
    id            = Column(Integer, primary_key=True, index=True)
    org_name      = Column(String, index=True, nullable=False)
    funder_name   = Column(String, nullable=False)
    program_name  = Column(String, nullable=False)
    score_final   = Column(Float, nullable=True)
    deadline      = Column(String, nullable=True)
    award_range   = Column(String, nullable=True)
    next_action   = Column(Text, nullable=True)
    run_date      = Column(DateTime, default=datetime.utcnow)
    raw_data      = Column(Text, nullable=True)